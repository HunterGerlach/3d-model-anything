# Android PR #2 security review

Review date: 2026-11-03. Target: Android capture on a personal phone, followed by local processing on Linux.

## Decision

**Do not merge the original PR unchanged.** The reviewed application source contains no observed covert upload mechanism or intentionally malicious code, but the PR introduces unsafe bundle-file handling and a deletion/privacy defect, and specifies an affected Gradle version. This branch incorporates the upstream changes with focused remediation and regression tests. It is a review candidate; phone testing and release signing remain separate steps.

This review is tied to the source below. A later upstream commit or dependency resolution needs its own review. Absence of an observed problem is not a guarantee that all third-party binaries or device behavior are safe.

## Exact scope and provenance

| Item | Reviewed value |
| --- | --- |
| Upstream PR | [wesbos/3d-model-anything #2](https://github.com/wesbos/3d-model-anything/pull/2), open draft at review time |
| Contributor branch | `JaredEzz/3d-model-anything:android-capture` |
| Reviewed PR head | `8cc9006cd557c3f2d6f267a220189345c08f8b13` |
| PR base and original fork main | `31b2dc9da3fb541e501f2f0936e55b32afb70ee8` |
| Destination fork | [HunterGerlach/3d-model-anything](https://github.com/HunterGerlach/3d-model-anything) |
| Original change size | 2 commits; 56 changed files; 7,247 additions and 6 deletions |
| Inspected upstream CI artifact | [Android workflow run 39833053470](https://github.com/JaredEzz/3d-model-anything/actions/runs/39833053470), exact reviewed head |

The review covered the Android Kotlin and XML sources, application and build configuration, three original Android test suites, the new Python synthesis/import path and affected reconstruction code, all changed workflows, package manifests/locks, and the merged manifest and packaged library version markers in the upstream CI APK. It included static source inspection, bounded temporary-file checks, advisory lookups, and regression testing. The CI APK was inspected as data; it was not installed or executed.

The original PR adds no custom native binaries, submodules, symlinks, or custom download-and-run hooks. Its normal build still fetches tools and dependencies. Changed text was checked for common credential patterns and bidirectional control characters, with no matches found. Those checks do not prove that no secret or obfuscation could exist.

### Artifact identity

The downloaded CI archive matched the SHA-256 supplied by GitHub's artifact metadata:

```text
Artifact ID: 12449357797
Artifact name: house-capture-android-debug
Archive SHA-256:
0efbd835db0adaf547a4a6d7c292062b76d0fcd82f66e91f5efe9e4d2bb8cbfc
APK SHA-256:
cdd36848f1432396f223b441f2e307370db4899813936080d842d4663e123064
```

This binds the inspected download to that artifact. It does not establish reproducible source-to-binary equivalence or independently verify the APK's signing identity.

## Findings and disposition

Severity below reflects the stated preconditions and this personal capture/import workflow. The build-tool advisory severity is the publisher's rating; project-specific reachability is qualified separately.

| ID | Priority | Finding in original PR | Origin | Candidate remediation |
| --- | --- | --- | --- | --- |
| PY-01 | High when importing a crafted bundle | Unvalidated manifest room index can influence output paths outside the selected bundle | New synthesis script | Require integer room IDs; validate all generated paths before processing |
| PY-02 | High/medium depending on reachable files | Predictable temporary files and symlink-following reads/backups can write outside the bundle or copy unrelated local content into it | New synthesis script; related reconstruction reads were inherited | Contained paths, regular-file reads, unique private temporary files, atomic replacement |
| PY-03 | Medium | Compressed depth and metadata-derived allocations lack useful bounds | New synthesis script; unbounded depth inflate also inherited in reconstruction | Exact bounded DEFLATE, dimensional/aggregate limits, pose and geometry validation, bounded PLY parsing and grids |
| AND-01 | Medium privacy | Deleting a room leaves complete exported ZIP copies in app cache | New Android application | Session-associated export cleanup, URI revocation, checked deletion, visible errors |
| BUILD-01 | High published advisories | Gradle 8.9 is within affected version ranges | New Android build instructions/workflow | Verified Gradle 8.14.6 wrapper/distribution |
| BUILD-02 | High Protobuf / low Commons IO publisher ratings; build-input dependent | AGP 8.7.3 selects affected Protobuf and Commons IO parsers | New Android build toolchain | AGP 8.13.2 selects patched Protobuf 3.25.5 and Commons IO 2.16.1 |
| BUILD-03 | Residual dependency maintenance | Other selected build libraries still match advisories | New Android build toolchain | Assessed task/configuration boundaries below; not claimed universally fixed |
| AND-02 | Low, conditional | `allowBackup=false` alone does not express an Android 12+ device-transfer exclusion | New Android configuration | Explicit cloud-backup and device-transfer exclusions |
| CI-01 | Hardening | Mutable action tags, checkout credentials, unpinned Python packages; Pages build shares deploy privileges | Mixed new and inherited configuration | SHA pins, no persisted checkout credential, hash-pinned CI wheels, separate build/deploy permissions |
| RELEASE-01 | Release gate | Published artifact is a debug APK | Intentional upstream CI behavior | Build and inspect unsigned release separately; sign locally before ordinary private use |

### PY-01: Room index is part of a filesystem path

In the [original `android_synthesize_room.py`](https://github.com/JaredEzz/3d-model-anything/blob/8cc9006cd557c3f2d6f267a220189345c08f8b13/.agents/skills/scan-to-threejs/scripts/android_synthesize_room.py), `--room` is parsed as an integer when supplied on the command line, but its default comes directly from `manifest.rooms[0].index`. That default is interpolated into room, backup, and mesh filenames without equivalent type validation or containment checks.

A crafted bundle can therefore affect paths outside its directory when the user processes it. A bounded temporary-directory check confirmed an out-of-bundle overwrite through the manifest-selected room path. This requires the user to process the crafted bundle with a writing operation; there is no demonstrated unsolicited remote exploit or automatic execution of a bundle.

The fix validates the room index both at the public `synthesize()` entry point and in the CLI default path, requires a non-negative 32-bit integer rather than a string/bool/float, and checks generated paths before reconstruction begins. The regression suite verifies rejection before scan data is loaded or output directories are created.

### PY-02: Symlinks and predictable temporary names cross the file boundary

The original new script opens `path + '.tmp'` and later replaces the destination. An existing link at that temporary filename can redirect the initial write. Frame/confidence paths are joined with the bundle root without checking containment; the room backup uses `shutil.copyfile()` on paths that may be symlinks. A pre-existing or deliberately prepared filesystem bundle can therefore redirect reads or writes. Copying an unrelated local file into a bundle could disclose it if that bundle is later shared; this is not an automatic network upload.

Temporary-directory checks confirmed the unsafe overwrite and backup-copy behavior using disposable test content. The remediation replaces predictable temporary names with newly created private files, validates destination and parent paths, rejects symlinks under the selected bundle root, and reads bounded regular files without following the final link. Atomic replacement also preserves an existing output when serialization fails.

The shared safeguards cover the changed synthesis path and the affected reconstruction frame/photo/output operations. They do not turn every existing repository tool, archive extractor, native mesh parser, or the host filesystem into a sandbox. They also do not claim to protect against a concurrent process already running as the same operating-system user and changing parent directories during processing.

### PY-03: Small or malformed input can request excessive work

The original loaders inflate an entire raw-DEFLATE frame before checking it against declared dimensions. In one deliberately bounded check, a 16,310-byte compressed fixture expanded to 16 MiB even though its metadata declared a 1-by-1 depth image. Metadata can also produce extreme raster dimensions; the review calculated a requested allocation above 700 GB and stopped before allocation. The latter is evidence of missing validation, not a claim that that much memory was actually consumed.

The fix checks dimensions and the selected frame budget before loading, limits compressed input, stops decompression after the exact expected size plus one byte, and rejects incomplete, concatenated, trailing, or oversized streams. Confidence bytes must match the pixel count and valid confidence range. Camera matrices must be finite rigid poses, intrinsics must be valid, scene bounds are limited, and raster/tile counts are checked before allocation. The PLY reader bounds the header, body, counts, properties, decoded arrays and indices; ASCII is processed by row rather than split into an unbounded token object graph. Depth point clouds are consolidated incrementally.

Current room-scan limits include 16 MiB JSON, 10,000 metadata records, 1,048,576 pixels per depth frame, 67,108,864 selected depth pixels in total, 4,194,304 cells per derived grid, a 200 m scene span, and a 128 MiB PLY input with at most 2 million vertices and 4 million faces. These limits are explicit compatibility choices for room capture. A large legitimate dataset may need resampling or a reviewed limit change. They bound important allocations; they are not a comprehensive CPU/RAM quota.

### AND-01: The deletion promise omitted exported copies

The [original store](https://github.com/JaredEzz/3d-model-anything/blob/8cc9006cd557c3f2d6f267a220189345c08f8b13/apps/house-capture-android/app/src/main/java/com/wesbos/housecapture/store/HouseStore.kt) deletes the session directory, while [sharing](https://github.com/JaredEzz/3d-model-anything/blob/8cc9006cd557c3f2d6f267a220189345c08f8b13/apps/house-capture-android/app/src/main/java/com/wesbos/housecapture/bundle/ZipShare.kt) creates an independently named complete ZIP under `cache/share`. The ZIP is created before the Android chooser, so cancelling sharing can leave a copy too. The one-day cleanup runs only when another export occurs; it is not an enforced expiry. The UI had promised deletion of the room's files from the phone, and deletion failures were ignored.

This is a confirmed source-level privacy defect, not an arbitrary-app escape from Android's sandbox. The FileProvider itself is narrowly scoped. Exposure depends on later access to the app's private storage or a still-authorized sharing recipient.

The candidate associates exports with validated session IDs, removes that session's local exports and revokes their URI grants during deletion, clears legacy unassociated exports, checks failures and shows an actionable error. Export/delete operations are coordinated so a concurrent share does not recreate a just-deleted export. The UI accurately describes app-held copies; a recipient's independently saved copy is outside this application's control. Revocation also cannot recall bytes already read or close a recipient's already-open file descriptor. Eleven new JVM tests cover the file lifecycle; actual Android URI-grant behavior still needs device verification.

### BUILD-01: Gradle 8.9 is affected

The PR's CI and README specify Gradle 8.9. [Gradle's 8.14.6 release notes](https://docs.gradle.org/8.14.6/release-notes.html) identify fixes for three high-rated security issues, including daemon-channel deserialization and TLS repository-fallback behavior. The [daemon advisory](https://github.com/gradle/gradle/security/advisories/GHSA-xwqc-3h47-hg64) includes versions below 8.14.6; it requires a same-host attacker and is explicitly not reachable through a remote network connection. These are build-machine risks, not evidence that the Android APK remotely executes code.

The candidate uses Gradle 8.14.6 with a committed wrapper, verifies the wrapper JAR, and pins the distribution checksum:

```text
7988ed071b2a07900e2ec715fca15c6ab72bce6db433af301fa7fa7e9407bd24
```

The official downloaded distribution was checked against the publisher's checksum. The wrapper JAR also matched the published SHA-256 `ad16bca46cb71bb5887c546860d93992aeee35c1fdaf4a242b0a612375d937e0`. `--no-daemon` alone is not treated as a security fix because Gradle may still create a single-use daemon for the requested JVM settings. Full dependency verification for every resolved Maven artifact remains a separate task; collecting first-seen hashes would not independently establish provenance.

### BUILD-02: Update the Android plugin's affected build parsers

Resolving the actual project showed AGP 8.7.3 selecting Protobuf Java 3.22.3 and Commons IO 2.13.0. Protobuf parses build resource metadata, making its malformed-input handling a meaningful build-side boundary. The [Protobuf maintainer advisory](https://github.com/protocolbuffers/protobuf/security/advisories/GHSA-735f-pc8j-v9w8) identifies a parsing denial of service and lists 3.25.5 among its fixes. [Apache's advisory](https://commons.apache.org/proper/commons-io/security.html) identifies excessive CPU consumption in the older XML stream reader and fixes it in 2.14.0; Apache rates that issue low, despite some database ratings being higher.

The candidate updates the Android Gradle Plugin to 8.13.2, which selects Protobuf 3.25.5 and Commons IO 2.16.1. This is a plugin update rather than forced overrides of individual transitive libraries. Google's [compatibility table](https://developer.android.com/build/releases/agp-8-13-0-release-notes) specifies Gradle 8.13 or newer, JDK 17 and Build Tools 35.0.0; the candidate's Gradle 8.14.6 meets that minimum and the README now specifies Build Tools 35. Both APK variants and the full JVM suite are required in CI after the update.

### AND-02: Android transfer behavior needs an explicit policy

The app disables backup, but captures live in normal application files. [Android's documentation](https://developer.android.com/about/versions/12/behavior-changes-12#backup-restore) states that some Android 12+ manufacturers still permit device-to-device transfer when `allowBackup=false`. The candidate adds explicit cloud-backup and device-transfer exclusions for the `filesDir` domain containing captures while retaining `allowBackup=false`. Future storage domains would require their own policy review.

This finding depends on OS/OEM behavior and a user-mediated device transfer. It was not reproduced on the user's phone and is not presented as a remote file disclosure.

### CI-01: Reduce build credentials and mutable dependencies

The candidate pins official GitHub Actions to full commit SHAs and disables persisted checkout credentials. The Android job has read-only repository permission. The existing Pages workflow now builds without Pages or OIDC deployment privileges and grants those only to its separate deployment job. The branch does not deploy a website merely by being opened as a PR.

The Python CI job uses exact CPython 3.12 Linux x86_64 wheel hashes for Pillow 13.0.0 and NumPy 2.5.4, checked against downloaded artifacts and PyPI release metadata. Publication provenance identifies the expected upstream workflows. Other platforms need separately reviewed hashes; the CI lock is deliberately platform-specific. The JavaScript lockfile was unchanged, uses npm registry integrity entries, and the lockfile-only advisory audit reported zero known vulnerabilities at review time.

These measures reduce substitution and credential exposure. They do not audit every line of third-party code or freeze the operating-system runner image and all remotely fetched transitive components.

## Android security-positive evidence

Both the source and inspected merged debug manifest request CAMERA as the only sensitive Android permission. The merged APK also contains an application-specific signature permission used for receiver handling. **It does not request INTERNET**, location, microphone, broad storage, contacts, SMS, accessibility, or package-install privileges.

The source FileProvider is non-exported, grants read URIs for an explicit chooser, and exposes only the export cache subtree. It does not expose the entire files directory. Session lookups canonicalize the root and target and require a direct child. The app defines no general bundle import, custom deep-link handler, exported service, or app-written intent dispatcher that reads caller-supplied filesystem paths.

Source inspection found no application-written network client, upload endpoint, WebView, runtime shell execution, dynamic DEX loader, custom JNI, analytics client, or hard-coded credential. The reviewed ARCore configuration uses local tracking/depth; Cloud Anchors and Geospatial APIs are not enabled. Camera permission and AR session resume/pause follow the scan destination; no background capture service is implemented.

The export intentionally includes interior photographs, depth/geometry, camera poses/intrinsics, device model, OS version, timestamps and capture diagnostics. The app does not collect GPS, a device serial or Android ID in the reviewed code. The only JPEG EXIF field explicitly written is orientation. Choosing a receiving cloud/share application can still send the exported contents to that service at the user's direction.

These observations concern the reviewed application. They do not certify Google Play Services for AR, Android media parsers, GPU drivers, or the operating system.

## Dependency and release qualifications

An advisory lookup covered 53 identifiable Maven coordinates recovered from APK version markers and the declared build/runtime coordinates. No listed advisory was returned for those 53 runtime queries. Version markers are an incomplete inventory, and a clean query is not proof of safety.

The initial build-cache scan was followed by actual Gradle selection reports. With AGP 8.7.3, the project selected 149 build coordinates, of which 15 matched 59 distinct advisory IDs; its 97 release-runtime coordinates returned no OSV matches. The [baseline dependency inventory](android-dependency-scan-2026-11-03.json) records those exact queries. The candidate's AGP 8.13.2 selection was resolved separately, including the existing Kotlin plugins: 152 build coordinates, 13 matched coordinates and 48 distinct IDs. The 97 release-runtime coordinate/version pairs are unchanged and still returned no listed matches. The [updated inventory](android-dependency-scan-agp8.13.2-2026-11-03.json) records the queries and catalog hash. These are scanner/version matches, not a count of demonstrated exploitable flaws in the app.

### BUILD-03: Residual build dependencies and applicability

The AGP update does not eliminate every advisory match. Exact cached AGP artifacts and the reviewed source paths support the following limited conclusions for the commands used here:

| Remaining component/family | Observed use or precondition | Disposition for this workflow |
| --- | --- | --- |
| Netty 4.1.110.Final / gRPC | UTP result listener for device and managed-device instrumentation tests; generated certificates, mandatory client authentication, `forPort` binding without an explicit loopback address | JVM unit tests and APK assembly do not request that task. No demonstrated activation of the affected server path by these commands. Do not extend that conclusion to instrumentation testing or describe the listener as loopback-only. |
| JDOM 2.0.6 | Jetifier contains a relevant XML parser, but Jetifier defaults to false and is not enabled in this project | Affected operation exists in the tooling; the required feature is disabled here. Reassess before enabling Jetifier. |
| Commons Compress 1.21 | Reviewed SDK installer consumers use ZIP APIs; the matched advisories concern DUMP/Pack200 processing | Those affected format paths were not found in the reviewed operations. Keep the library maintenance issue open. |
| jose4j 0.9.5 | Bundletool uses JWS code-transparency operations; the matched path concerns JWE decryption/decompression | No demonstrated use of the affected operation in unit tests or debug/unsigned-release assembly. |
| Bouncy Castle 1.79 | Reviewed build paths create local RSA/SHA256 certificates; matched issues involve other algorithms, validation, CRL/LDAP or composite-signature paths | No demonstrated affected path in the reviewed build operations. This is not a review of every signing/crypto feature. |
| Guava 33.3.1-jre | The [publisher advisory](https://github.com/google/guava/security/advisories/GHSA-xxph-c9ww-hj94) concerns native Java deserialization of particular collections | No hostile object-stream entry point to those collection readers was demonstrated here. The library still matches the advisory; Google rates it moderate. |
| Kotlin Gradle plugin 2.0.21 | KAPT incremental-cache deserialization | KAPT is not applied; details below. |

Netty remains old enough for later [HTTP/2 advisories](https://github.com/netty/netty/security/advisories/GHSA-prj3-ccx8-p6x4); mandatory client authentication is a condition of the observed listener, not a universal patch for Netty. These findings justify continued toolchain maintenance and a new review when enabling additional build features. All listed families are absent from the selected application release runtime graph; `guava:listenablefuture` is a separate small runtime artifact, not the affected full Guava library.

No reachable high/critical exploit was demonstrated in the reviewed JVM-test plus APK-assembly commands. That is a scoped assessment based on the observed tasks and configurations, not proof that every dependency method is unreachable or vulnerability-free. The project still lacks independently reviewed complete dependency verification/locking. Prefer a compatible supported toolchain update over arbitrary per-module overrides beneath AGP.

Kotlin 2.0.21 matches the published affected version range for [GHSA-r937-wjx7-w2jp / CVE-2026-53914](https://github.com/advisories/GHSA-r937-wjx7-w2jp). The linked [official fix](https://github.com/JetBrains/kotlin/commit/bf51df665b458fda7c3eaf436c4d88dc119d7ec6) concerns KAPT incremental-cache deserialization. This project does not apply KAPT or declare annotation processors, so that specific vulnerable path was not shown to be active. It remains a dependency follow-up; the review does not claim the advisory is fixed or turn a version match into demonstrated APK compromise. A compatible stable Kotlin/Compose/toolchain update should be evaluated separately rather than blindly selecting the advisory's beta minimum.

The inspected upstream APK is `debuggable=true` and includes an exported Compose tooling `PreviewActivity`. The app declares that tooling as a debug-only dependency. No sensitive-data exploit through that activity was established, but an authorized ADB/debugging host can inspect a debug app and its storage. The candidate CI builds both variants and rejects a release merged manifest containing INTERNET, debugging, or the Compose preview activity. The release artifact is intentionally **unsigned**. It must be signed with the owner's locally controlled key before installation; CI should not receive that private key for this prototype.

## Verification and remaining gates

The original Python suite passed all 15 tests before changes. The remediated suite passed 34 tests locally, including 19 new bounded security regressions. An independent follow-up review found no remaining blocker in the defensive corrections and reran all 19 security tests plus the 8 Android synthetic cases successfully. Existing synthetic-room tests still check four-wall geometry, door/window candidates, object candidates, the mesh schema and downstream conversion. This validates compatibility on generated fixtures, not physical scan accuracy.

The first patched candidate, `487ab106b7a74321ad9f2624e2814c05ebb93ad7`, passed its [actual GitHub Android build](https://github.com/HunterGerlach/3d-model-anything/actions/runs/41718306576): 41 JVM tests, zero failures/errors/skips, debug assembly, unsigned release assembly, and the release-manifest gate. The test artifact and release archive matched GitHub's supplied SHA-256 digests. Independent inspection of the built release APK confirmed no INTERNET permission, no debuggable flag, no Compose preview activity, a non-exported FileProvider, `allowBackup=false`, and compiled cloud/transfer exclusions. Its [verification record](android-ci-verification-487ab10.json) includes hashes and test counts. That run used AGP 8.7.3; the subsequent 8.13.2 update is checked again by the same CI gates. See the [draft PR's current checks](https://github.com/HunterGerlach/3d-model-anything/pull/1/checks) for the exact final candidate result.

Run the complete Python suite from the repository root:

```sh
python3 -m unittest discover -s .agents/skills/scan-to-threejs/scripts -p 'test_*.py'
```

Run the Android gates from `apps/house-capture-android` with the documented SDK and JDK:

```sh
./gradlew --no-daemon :app:testDebugUnitTest :app:assembleDebug :app:assembleRelease
```

Final build/check outcomes are recorded with the candidate PR. Do not infer a successful patched build from the upstream run: that run proves only the original head passed its original jobs. The local environment initially had no Android SDK or Gradle and its Java runtime lacked a compiler, so the successful full compilation evidence comes from GitHub's JDK 17 runner. Official locally downloaded tooling was checksum-verified; local dependency selection and Python tests were separate from that full CI build.

Before routine private-room use:

1. Require successful checks for the exact candidate commit, including the release manifest gate.
2. Review and establish full resolved Android dependency verification/locking if stronger supply-chain assurance is required; do not describe generated first-use checksums as independent verification.
3. Sign the reviewed non-debug release locally. Keep signing material out of the repository and CI artifacts.
4. On the actual Android phone, test one non-sensitive scene: camera permission/lifecycle, usable raw depth, export, cancelled sharing, deletion, repeated export/delete, and any visible failure behavior.
5. Import that test scan on Linux and compare dimensions and candidate geometry to the scene before relying on a full reconstruction.

Physical-device ARCore depth quality, the earlier reported zero-depth capture issue, dimensions/geometry accuracy, and OEM backup behavior remain device tests. Open3D native reconstruction was not fully exercised by the NumPy/Pillow synthetic suite. This security review supplies a safer starting point and explicit remaining gates; it is not a claim of a finished production scanner.
