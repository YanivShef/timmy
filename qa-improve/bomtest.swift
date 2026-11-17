import Foundation
let d = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
print("swift parses BOM json:", (try? JSONSerialization.jsonObject(with: d)) != nil)
