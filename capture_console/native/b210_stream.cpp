// b210_stream.cpp - tune a USRP once and stream coherent multi-channel IQ to stdout.
//
// Output: back-to-back blocks, each block = for ch in channels: N complex64 (fc32) samples.
// i.e. block layout [ch0 x N][ch1 x N]... so a reader can np.frombuffer(...).reshape(n_ch, N).
// Header lines go to stderr. Used by live_radar_capture.py because this machine's UHD
// install has no Python bindings.
//
// The USB receive loop never waits for the reader: blocks go through a FIFO (~2 s deep) to a
// separate writer thread. A slow reader therefore costs nothing until the FIFO is full; only
// then whole blocks are dropped (counted as dropped=). Before this, any pause of the reader
// longer than a few ms overflowed the USB buffers ("O") and lost up to a block each time.
//
// Hopping (--hop f1,f2,f3 --dwell N): after every N blocks the streamer retunes to the next
// frequency, throws away the samples received while the LO settles (by sample time stamp), and
// starts a fresh block. So every output block is entirely ONE frequency and block k is on
// frequency index (k / N) % len(hop) - the reader just counts blocks.
//
// Build: clang++ -std=c++17 -O2 b210_stream.cpp -o b210_stream \
//          -I$(brew --prefix uhd)/include -I$(brew --prefix boost)/include \
//          -L$(brew --prefix uhd)/lib -luhd
#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/stream.hpp>
#include <uhd/types/tune_request.hpp>
#include <complex>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <chrono>
#include <mutex>
#include <condition_variable>
#include <deque>
#include <uhd/utils/thread.hpp>

static volatile bool stop_flag = false;
static void on_sig(int) { stop_flag = true; }

int main(int argc, char** argv) {
    std::string args = "";
    // Over-the-wire format. sc12 carries the full 12 bits of the B210's ADC in 3/4 of the USB
    // bytes of sc16. On this Mac, 2 channels x 4 MS/s as sc16 overflowed ~9 times a second
    // (even writing to /dev/null); as sc12 it runs clean. --otw sc16 restores the old format.
    std::string otw = "sc12";
    std::string antenna = "TX/RX";   // port the antennas are screwed onto (TX/RX or RX2)
    double freq = 2.426e9, rate = 4e6, gain = 50, bw = 0;
    size_t nsamps = 40000;
    std::vector<size_t> chans = {0, 1};
    std::vector<double> hop;          // frequencies to cycle through (empty = fixed --freq)
    size_t dwell = 0;                 // blocks per hop frequency
    double settle = 0.003;            // s discarded after each retune
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() { return std::string(i + 1 < argc ? argv[++i] : ""); };
        if (a == "--args") args = next();
        else if (a == "--freq") freq = std::stod(next());
        else if (a == "--rate") rate = std::stod(next());
        else if (a == "--gain") gain = std::stod(next());
        else if (a == "--bw") bw = std::stod(next());
        else if (a == "--antenna") antenna = next();
        else if (a == "--otw") otw = next();
        else if (a == "--block") nsamps = std::stoul(next());
        else if (a == "--dwell") dwell = std::stoul(next());
        else if (a == "--settle") settle = std::stod(next());
        else if (a == "--hop") {
            std::string s = next(); size_t p = 0;
            while (p < s.size()) { size_t q = s.find(',', p); if (q == std::string::npos) q = s.size();
                hop.push_back(std::stod(s.substr(p, q - p))); p = q + 1; }
        }
        else if (a == "--channels") {
            chans.clear();
            std::string s = next(); size_t p = 0;
            while (p < s.size()) { size_t q = s.find(',', p); if (q == std::string::npos) q = s.size();
                chans.push_back(std::stoul(s.substr(p, q - p))); p = q + 1; }
        }
    }
    if (hop.size() < 2 || dwell == 0) hop.clear();
    if (!hop.empty()) freq = hop[0];
    std::signal(SIGINT, on_sig); std::signal(SIGTERM, on_sig); std::signal(SIGPIPE, on_sig);

    // Larger USB receive queue: rides out scheduling hiccups of this process.
    if (args.find("num_recv_frames") == std::string::npos)
        args += (args.empty() ? "" : ",") + std::string("num_recv_frames=512");
    auto usrp = uhd::usrp::multi_usrp::make(args);
    if (chans.size() > 1) usrp->set_rx_subdev_spec(uhd::usrp::subdev_spec_t("A:A A:B"));
    usrp->set_rx_rate(rate);
    // Timed tune so both channels' LO/DSP settle together (B210 shares one LO -> coherent).
    usrp->set_time_now(uhd::time_spec_t(0.0));
    for (size_t c : chans) {
        usrp->set_rx_freq(uhd::tune_request_t(freq), c);
        usrp->set_rx_gain(gain, c);
        usrp->set_rx_antenna(antenna, c);
        if (bw > 0) usrp->set_rx_bandwidth(bw, c);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    std::cerr << "[b210_stream] rate=" << usrp->get_rx_rate() / 1e6 << " MHz freq="
              << usrp->get_rx_freq(chans[0]) / 1e9 << " GHz gain=" << usrp->get_rx_gain(chans[0])
              << " ant=" << usrp->get_rx_antenna(chans[0]) << " chans=" << chans.size() << " block=" << nsamps << " otw=" << otw;
    if (!hop.empty()) { std::cerr << " hop="; for (double f : hop) std::cerr << f / 1e9 << ","; std::cerr << " dwell=" << dwell; }
    std::cerr << std::endl;

    uhd::stream_args_t sa("fc32", otw);
    sa.channels = chans;
    auto rx = usrp->get_rx_stream(sa);
    uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
    cmd.stream_now = false;
    cmd.time_spec = usrp->get_time_now() + uhd::time_spec_t(0.1);
    rx->issue_stream_cmd(cmd);

    // FIFO of blocks between the receive loop (this thread) and the stdout writer thread.
    typedef std::vector<std::complex<float>> block_t;          // [ch0 x N][ch1 x N]...
    const size_t blk_len = nsamps * chans.size();
    const size_t n_blocks = std::max<size_t>(8, (size_t)(2.0 * rate / nsamps));
    std::vector<block_t> pool(n_blocks, block_t(blk_len));
    std::deque<block_t*> free_q, full_q;
    for (auto& b : pool) free_q.push_back(&b);
    std::mutex mtx;
    std::condition_variable cv;
    bool done = false;

    std::thread writer([&]() {
        for (;;) {
            block_t* b;
            {
                std::unique_lock<std::mutex> lk(mtx);
                cv.wait(lk, [&] { return !full_q.empty() || done; });
                if (full_q.empty()) return;
                b = full_q.front(); full_q.pop_front();
            }
            bool ok = fwrite(b->data(), sizeof(std::complex<float>), blk_len, stdout) == blk_len;
            fflush(stdout);
            { std::lock_guard<std::mutex> lk(mtx); free_q.push_back(b); }
            if (!ok) { stop_flag = true; return; }
        }
    });

    uhd::set_thread_priority_safe(1.0, true);
    std::vector<void*> ptrs(chans.size());
    uhd::rx_metadata_t md;
    size_t overflows = 0, dropped = 0, rep_ovf = 0, rep_drop = 0;
    auto last_rep = std::chrono::steady_clock::now();
    block_t scratch(blk_len);
    size_t hop_i = 0, in_dwell = 0, n_tunes = 0;
    double tune_ms_max = 0;
    while (!stop_flag) {
        block_t* b = nullptr;
        { std::lock_guard<std::mutex> lk(mtx); if (!free_q.empty()) { b = free_q.front(); free_q.pop_front(); } }
        block_t& dst = b ? *b : scratch;                     // FIFO full -> receive anyway, then drop
        size_t got = 0;
        while (got < nsamps && !stop_flag) {
            for (size_t c = 0; c < chans.size(); c++) ptrs[c] = dst.data() + c * nsamps + got;
            size_t n = rx->recv(ptrs, nsamps - got, md, 1.0);
            // An overflow is a short gap in the stream: keep what we have and carry on
            // (it can break one packet; discarding the block would lose all of them).
            if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW) overflows++;
            else if (md.error_code != uhd::rx_metadata_t::ERROR_CODE_NONE &&
                     md.error_code != uhd::rx_metadata_t::ERROR_CODE_TIMEOUT)
                std::cerr << "[b210_stream] error: " << md.strerror() << std::endl;
            got += n;
        }
        if (stop_flag) break;
        if (b) { { std::lock_guard<std::mutex> lk(mtx); full_q.push_back(b); } cv.notify_one(); }
        else dropped++;
        if (!hop.empty() && ++in_dwell >= dwell) {
            in_dwell = 0;
            hop_i = (hop_i + 1) % hop.size();
            auto t0 = std::chrono::steady_clock::now();
            for (size_t c : chans) usrp->set_rx_freq(uhd::tune_request_t(hop[hop_i]), c);
            double tms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
            if (tms > tune_ms_max) tune_ms_max = tms;
            const double t_ok = usrp->get_time_now().get_real_secs() + settle;
            // drain everything sampled before the LO had settled
            for (;;) {
                for (size_t c = 0; c < chans.size(); c++) ptrs[c] = scratch.data() + c * nsamps;
                size_t n = rx->recv(ptrs, std::min<size_t>(nsamps, 1000), md, 1.0);
                if (stop_flag || (n && md.has_time_spec && md.time_spec.get_real_secs() >= t_ok)) break;
                if (!n && md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT) break;
            }
            if (++n_tunes == 3 || n_tunes % 3000 == 0)
                std::cerr << "[b210_stream] hopping ok: " << n_tunes << " retunes, slowest " << tune_ms_max << " ms" << std::endl;
        }
        auto now = std::chrono::steady_clock::now();
        if ((overflows != rep_ovf || dropped != rep_drop) && now - last_rep > std::chrono::seconds(5)) {
            std::cerr << "\n[b210_stream] LOSING SAMPLES: overflows=" << overflows << " dropped_blocks=" << dropped
                      << (dropped != rep_drop ? "  (reader too slow)" : "") << std::endl;
            rep_ovf = overflows; rep_drop = dropped; last_rep = now;
        }
    }
    { std::lock_guard<std::mutex> lk(mtx); done = true; full_q.clear(); }
    cv.notify_one();
    writer.join();
    rx->issue_stream_cmd(uhd::stream_cmd_t(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS));
    std::cerr << "[b210_stream] stopped, overflows=" << overflows << " dropped_blocks=" << dropped << std::endl;
    return 0;
}
