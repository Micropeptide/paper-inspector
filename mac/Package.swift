// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "PaperInspector",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "PaperInspector", path: "Sources/PaperInspector"),
        .testTarget(name: "PaperInspectorTests", dependencies: ["PaperInspector"], path: "Tests/PaperInspectorTests"),
    ],
    swiftLanguageVersions: [.v5]
)
