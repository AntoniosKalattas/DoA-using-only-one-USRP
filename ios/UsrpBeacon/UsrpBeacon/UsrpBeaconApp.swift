import SwiftUI

@main
struct UsrpBeaconApp: App {
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(model)
        }
    }
}
