# AI Control Mesh for Android

Watch and drive your coding agents from your phone. The app keeps a list of your
agent-servers and opens each one's console full screen, on **Running agents**: read an
agent's screen, answer its prompts, start agents on your machines, and see the traffic.

It's a native shell around the console (`../ui`): the console is built for phones, so
every improvement there shows up in the app, with no second UI to maintain.

Debug builds also include a private Android Auto session monitor; see below.

- **Your servers.** Add a server by its address (`http://192.168.1.10:9999`, a Tailscale
  name, or HTTPS). The app checks it's an agent-server before saving it, and shows
  whether each is online.
- **Signing in** is mandatory OIDC. The app discovers the chosen API's HTTPS issuer and
  keeps its sign-in and API callback in the WebView, on this phone only. OAuth access
  and refresh cookies are not backed up; legacy pairing codes no longer grant access.
- **Plain HTTP** is allowed, because agent-servers usually run on a LAN or Tailscale; the
  app says when a connection isn't encrypted. HTTPS with a certificate you installed on
  the phone works too.
- **Back** goes through the console's own history (closing its menu first), then to your
  servers. Links to other sites open in the browser. A redirect from HTTP to HTTPS on the
  same server stays in the app.
- **Separate console hosting.** An initial server redirect to its HTTPS console stays
  inside the app, even on a different host. Later links to other sites still open in
  the browser. For Android Auto, save the API server's address, where sign-in cookies
  are held (for example, `https://api.example.com`).
- **Updates.** Each server says its version (`/health`). When it isn't the app's own, its
  card says **Update available** and **Update** opens that server's **Downloads** page
  (`/get`), which offers the app built for it; tapping the APK hands it to Android to
  download and install. Files the console offers are always downloaded by the system, as
  a WebView can't save them itself.

## Getting it

Open `http://<your server>/android` in the phone's browser: every agent-server hands out
the app built with it there, no sign-in needed. Its Downloads page (`/get`) has it too,
with its size and SHA-256.

## Building

`scripts/build-dist.sh` (`.\scripts\build-dist.ps1`) builds the APK with everything else,
into `dist/<version>/android/`, when it finds the SDK and a JDK; the server then offers
it on its Downloads page. The app's version is the project's (`../VERSION`), or the build's
own when tools/dist passes `-PappVersion=0.4.0+56458f9`; `versionCode` comes from its
`X.Y.Z` (0.4.0 is 400). To build it by hand:

You need the Android SDK (Android Studio installs it) and JDK 17 or 21. Point Gradle at
the SDK in `local.properties` (`sdk.dir=C\:/Users/you/AppData/Local/Android/Sdk`, or set
`ANDROID_HOME`), then:

```bash
./gradlew assembleDebug        # app/build/outputs/apk/debug/app-debug.apk (-PappVersion=... to stamp a version)
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

For a release build, create a key once
(`keytool -genkeypair -v -keystore release.jks -alias agentproxy -keyalg RSA -keysize 2048 -validity 10000`)
and a `keystore.properties` beside this file (both are gitignored):

```properties
storeFile=release.jks
storePassword=...
keyAlias=agentproxy
keyPassword=...
```

then `./gradlew assembleRelease`.

Debug builds allow `chrome://inspect` on the console's WebView, which helps when a page
looks different on the phone.

## Private Android Auto experiment

Only `assembleDebug` includes the car app. Release builds have no car service or car
library. This is a private development experiment, not navigation and not eligible
for Play distribution as a navigation app. Its debug manifest uses the navigation
category solely so an Android Auto development host can discover it; a particular
host may still refuse it.

Add your servers and sign in in the phone app while parked. Connect a development host and
open **AI Control Mesh (private test)**. The picker shows the first six saved servers.
Choose a server to see up to five recent
sessions, with open sessions first: agent, project name, model, open/ended/error status
and request count. **Open** means the session has not ended, not that the agent is
currently generating. It refreshes every 15 seconds while visible, or with **Refresh**.
There are no terminals, prompts, launch buttons or typing controls.

The monitor reuses the phone's sign-in and the server's sharing permissions. An expired
sign-in needs renewing on the phone while parked. The phone must still be able to
reach your server (for example over Tailscale); Android Auto supplies no server access.
Do setup and testing while parked, never operate the phone while driving.

For development, enable Android Auto developer mode and test using Google's
[Desktop Head Unit](https://developer.android.com/training/cars/testing/dhu):
install the debug APK on a phone, start Android Auto's head unit server, run
`adb forward tcp:5277 tcp:5277`, then run the SDK's `desktop-head-unit.exe`.
No device or head-unit validation is implied by a successful Gradle build.

If the app is missing from the Android Auto launcher:

- Check the installed variant: only `app-debug.apk` contains `CarSessionsService`.
  A release APK cannot appear, even with developer mode enabled. The distribution
  builder chooses release when `android/keystore.properties` exists, debug otherwise.
- **Unknown sources alone does not enable sideloaded Car App Library apps in real vehicles.**
  Google's [testing requirements](https://developer.android.com/training/cars/testing#test-in-real-vehicles)
  require a trusted installation source for those apps, such as Play Internal App
  Sharing or an Internal Test Track. This monitor uses that library. The navigation
  category alone does not bypass host discovery or make it eligible for publication.
- Use the Desktop Head Unit to investigate the private experiment. If it still fails,
  capture `adb logcat` while connecting and opening the app; distinguish host discovery
  or authorization failures from a `CarSessionsService` exception. An empty session
  list or sign-in error after opening is a separate server-access issue.

### Unofficial real-car testing without Play uploads

These are reported workarounds, not validated on this app or guaranteed across phones:

- [KingInstaller](https://github.com/fcaronte/KingInstaller) installs the original APK
  through the system installer with Play Store installer metadata. Try its classic
  method first, then its Shizuku method if necessary. The
  [allEQ developer reports real-car success](https://github.com/omixin/allEQ#alleq-does-not-show-up-in-the-car)
  with its Car App Library screen on Android Auto 18. Download this project's debug
  APK, select it in KingInstaller, then check Android Auto's Customize launcher.
  Install each update the same way; ordinary browser/ADB installation can replace
  the installer metadata. Try updating in place first to preserve servers and cookies.
- KingInstaller's diagnostics should show the system Package Installer as requester
  and Play Store as installer. Its current README describes vendor restrictions,
  including cases on Xiaomi/POCO/Redmi where non-root Shizuku is insufficient.
- AAWireless developer mode is another
  [reported real-car route](https://github.com/AndreyPavlenko/Fermata/issues/846), with
  passthrough disabled. That investigation tested projection apps; applicability to
  this monitor still needs device testing. Google can change host validation remotely.
- A plain `adb install -i com.android.vending` is not equivalent: Android Auto can
  check the initiating installer too. Do not treat the displayed installer alone
  as proof of acceptance. Capture host logs if the app remains missing.

Run parser tests with `.\gradlew.bat testDebugUnitTest`; run Android lint with
`.\gradlew.bat lintDebug`.

## Layout

```text
app/src/main/java/dev/agentproxy/app/
  ServersActivity.kt   the servers list: add (checked against /health), remove, reopen the last one, update notices
  ConsoleActivity.kt   one server's console in a WebView: sign-in cookie, Back, offline screen, external links, downloads
  Servers.kt           the stored servers, and checking an address is an agent-server (and its version)
  Insets.kt            keeping content clear of the system bars (Android 15 draws edge to edge)
app/src/main/res/      layouts, the console's colours (light and dark), the network security config
app/src/debug/        private Android Auto service, session metadata parser, manifest and resources
app/src/testDebug/    car session parser tests
```
