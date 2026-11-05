# Steam Manifest Downloader - Android Edition 🎮📱

A port of **Steam Manifest Downloader (Python Edition)** into a native **Android application** powered by **Chaquopy (embedded CPython 3.11)**, featuring rich native Android system integrations and background download capabilities.

---

## 🌟 Android System Features & Updates Added

This port brings deep integration with the Android operating system:

1. **Foreground Download Service & Real-Time Notifications**:
   - **Live Progress Bar**: Ongoing notification showing exact download percentage (`0% - 100%`).
   - **Live Speed & Size**: Displays current download speed (e.g. `24.5 MB/s`) and progress (`1.4 GB / 8.2 GB`).
   - **Depot Tracker**: Tracks current depot progress (e.g., `Depot 2/5`).
   - **In-Notification Cancel Action**: Tap **Cancel** right from the notification shade to safely halt the download and preserve resume journals.

2. **Completion & Error Alerts (Notification Channels)**:
   - **Download Complete Notification**: Alert with sound and haptic vibration when your game is ready. Tapping the notification opens the download directory in your file manager.
   - **Download Failed / Error Notification**: Notifies if connection fails or depot key is invalid, saving resume progress for next attempt.

3. **WakeLock & WifiLock (Sleep Prevention)**:
   - Acquires `PowerManager.PARTIAL_WAKE_LOCK` and `WifiManager.WIFI_MODE_FULL_HIGH_PERF` during active downloads.
   - Prevents Android from putting the CPU or Wi-Fi chip into power-save sleep when the screen turns off.

4. **Storage Access Framework (SAF) Directory Picker**:
   - Replaces desktop prompt dialogs with Android's native `ACTION_OPEN_DOCUMENT_TREE` folder picker.
   - Easily choose your preferred download directory (internal storage, SD card, or USB OTG).
   - Defaults to `/storage/emulated/0/Download/SteamDownloads`.

5. **Native File Manager Integration**:
   - Tapping **"Open Folder"** in history or completed downloads launches the Android system file manager (Files app) directly to the game directory using Android Intents.

6. **Battery Optimization Exemption Prompt**:
   - Includes a helper to request battery optimization whitelist (`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) so aggressive OEM task managers (Samsung OneUI, Xiaomi MIUI, etc.) won't kill long downloads.

7. **Native Haptic Feedback (Vibration)**:
   - Subtle tactile feedback on completion, errors, and button interactions.

8. **Optimized Mobile WebView & Edge-to-Edge Dark UI**:
   - Seamless dark theme (`#171a21`) matching Steam's interface.
   - Automatically hides desktop window controls (minimize/maximize/close) on Android for full screen immersion.
   - Hardware acceleration and smooth touch scrolling.

9. **Fast Native AES Decryption**:
   - Uses Chaquopy's pre-compiled C-extension wheel for `pycryptodome` across all Android architectures (`arm64-v8a`, `armeabi-v7a`, `x86_64`), enabling hardware-accelerated decryption of Steam depot chunks.

---

## 🚀 How to Build on GitHub (Recommended)

This repository includes a pre-configured **GitHub Actions CI/CD workflow** (`.github/workflows/build-apk.yml`) that builds the APK in the cloud.

### Step 1: Push this repository to GitHub
```bash
git init
git add .
git commit -m "Port Steam Manifest Downloader to Android with native notifications"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

### Step 2: Download the built APK from GitHub Actions
1. Go to your GitHub repository in your web browser.
2. Click on the **Actions** tab at the top.
3. Select the latest workflow run: **Build Android APK**.
4. Scroll down to the **Artifacts** section:
   - Click **SteamManifestDownloader-Debug** to download `app-debug.apk`.
   - Click **SteamManifestDownloader-Release** to download `app-release-unsigned.apk`.

### Step 3 (Optional): Create an automatic GitHub Release
Push a version tag to trigger a release with APKs attached:
```bash
git tag v1.5.0
git push origin v1.5.0
```
GitHub Actions will automatically build the APKs and attach them to the **Releases** page of your repository!

---

## 💻 How to Build Locally

### Prerequisites
- **JDK 17** (Temurin or OpenJDK)
- **Android SDK** (API 34)

### Build Commands

#### On Linux / macOS:
```bash
# Make Gradle wrapper executable
chmod +x gradlew

# Build Debug APK
./gradlew assembleDebug

# Or build Release APK
./gradlew assembleRelease
```

The APK will be generated at:
- `app/build/outputs/apk/debug/app-debug.apk`
- `app/build/outputs/apk/release/app-release-unsigned.apk`

#### In Android Studio:
1. Open Android Studio.
2. Select **Open** and choose this project folder.
3. Wait for Gradle sync to complete.
4. Click **Build** > **Build Bundle(s) / APK(s)** > **Build APK(s)**.

---

## 🖥️ Running on Desktop (Linux / SteamOS / Windows)

The project maintains 100% backward compatibility with desktop environments!

To run on Linux, SteamOS, or macOS:
```bash
python3 app.py
# or
./start.sh
```

---

## 📁 Project Architecture

```
steam_manifest_downloader/
├── .github/workflows/
│   └── build-apk.yml               # GitHub Actions CI workflow to build APKs
├── app/
│   ├── build.gradle                # Chaquopy & Android dependencies
│   ├── proguard-rules.pro
│   └── src/main/
│       ├── AndroidManifest.xml     # Permissions, Foreground Service, Provider
│       ├── java/com/steammanifestdownloader/
│       │   ├── MainActivity.kt     # Full-screen WebView, SAF picker, permissions
│       │   ├── SteamDownloaderApp.kt  # Chaquopy runtime & backend server starter
│       │   ├── SteamDownloaderService.kt # Foreground service, WakeLock, notifications
│       │   ├── AndroidBridge.kt    # JS interface for folder picking, open, vibrate
│       │   ├── AndroidBridgeManager.kt # Direct Python-to-Kotlin event dispatcher
│       │   ├── NotificationHelper.kt  # Progress bars & alert channels
│       │   └── FileUtils.kt        # Android storage paths & file manager intents
│       ├── python/
│       │   └── app.py              # Python download engine with Android hooks
│       ├── assets/web/             # HTML5/CSS/JS frontend bundled inside APK
│       └── res/                    # Icons, themes, colors, and layout XMLs
├── build.gradle                    # Root Gradle configuration
├── settings.gradle                 # Chaquopy repository and Gradle plugins
├── gradlew / gradlew.bat           # Official Gradle wrapper scripts
└── README.md
```

---

## 📜 License
GPL-3.0 / MIT.
