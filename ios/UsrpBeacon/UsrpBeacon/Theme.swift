import SwiftUI

/// Same dark look as the Android app (cards, label + control + plain-English note).
enum Theme {
    static let bg = Color(rgb: 15, 17, 21)
    static let card = Color(rgb: 26, 29, 36)
    static let field = Color(rgb: 36, 40, 49)
    static let textMain = Color(rgb: 237, 239, 243)
    static let textSub = Color(rgb: 154, 163, 178)
    static let textNote = Color(rgb: 112, 120, 135)
    static let accent = Color(rgb: 79, 140, 255)
    static let green = Color(rgb: 34, 170, 90)
    static let amber = Color(rgb: 214, 150, 30)
    static let red = Color(rgb: 214, 70, 70)
    static let grey = Color(rgb: 70, 76, 88)
    static let slate = Color(rgb: 48, 56, 72)
}

extension Color {
    init(rgb r: Double, _ g: Double, _ b: Double) { self.init(red: r / 255, green: g / 255, blue: b / 255) }
}

struct Card<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title).font(.system(size: 17, weight: .semibold)).foregroundColor(Theme.textMain)
            if let subtitle {
                Text(subtitle).font(.system(size: 13)).foregroundColor(Theme.textSub).padding(.top, 4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(EdgeInsets(top: 16, leading: 18, bottom: 18, trailing: 18))
        .background(RoundedRectangle(cornerRadius: 16).fill(Theme.card))
    }
}

struct Setting<Control: View>: View {
    let label: String
    let note: String
    @ViewBuilder let control: Control
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 14, weight: .semibold)).foregroundColor(Theme.textMain)
            control
            Note(note)
        }.padding(.top, 18)
    }
}

struct Note: View {
    let text: String
    var color: Color = Theme.textNote
    var size: CGFloat = 12
    init(_ t: String, color: Color = Theme.textNote, size: CGFloat = 12) { text = t; self.color = color; self.size = size }
    var body: some View {
        Text(text).font(.system(size: size)).foregroundColor(color).fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct FieldBox: ViewModifier {
    func body(content: Content) -> some View {
        content.font(.system(size: 16)).foregroundColor(Theme.textMain)
            .padding(.horizontal, 14).padding(.vertical, 12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.field))
    }
}

struct InputField: View {
    @Binding var text: String
    var keyboard: UIKeyboardType = .default
    var body: some View {
        TextField("", text: $text)
            .keyboardType(keyboard)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .modifier(FieldBox())
    }
}

struct Picker2: View {
    let items: [String]
    @Binding var selection: Int
    var body: some View {
        Menu {
            ForEach(items.indices, id: \.self) { i in
                Button(items[i]) { selection = i }
            }
        } label: {
            HStack {
                Text(items.indices.contains(selection) ? items[selection] : "").foregroundColor(Theme.textMain)
                Spacer()
                Image(systemName: "chevron.up.chevron.down").foregroundColor(Theme.textSub)
            }.modifier(FieldBox())
        }
    }
}

struct BigButton: View {
    let label: String
    let color: Color
    var enabled: Bool = true
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(label).font(.system(size: 16, weight: .semibold)).foregroundColor(.white)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity).padding(.vertical, 14)
                .background(RoundedRectangle(cornerRadius: 12).fill(color))
        }
        .buttonStyle(.plain)
        .disabled(!enabled).opacity(enabled ? 1 : 0.35)
    }
}

struct Readout: View {
    let name: String
    let value: String
    let note: String
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                Text(name).font(.system(size: 14)).foregroundColor(Theme.textSub).frame(width: 110, alignment: .leading)
                Text(value).font(.system(size: 15, weight: .semibold, design: .monospaced)).foregroundColor(Theme.textMain)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            Note(note)
        }.padding(.top, 14)
    }
}

struct Diagram: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 12, design: .monospaced)).foregroundColor(Theme.textSub)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12).padding(.vertical, 10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.field))
    }
}
