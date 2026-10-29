# AI Control Mesh for Android

Watch and drive your coding agents from your phone. The app keeps a list of your
agent-servers and opens each one's console full screen, on **Running agents**: read an
agent's screen, answer its prompts, start agents on your machines, and see the traffic.

It's a native shell around the console (`../ui`): the console is built for phones, so
every improvement there shows up in the app, with no second UI to maintain.

- **Your servers.** Add a server by its address (`http://192.168.1.10:9999`, a Tailscale
  name, or HTTPS). The app checks it's an agent-server before saving it, and shows
  whether each is online.
- **Signing in** is the console's own: an account on a server with accounts, or pairing
  with a code on one without. The sign-in stays in the app's WebView, on this phone only
  (nothing is backed up).
- **Plain HTTP** is allowed, because agent-servers usually run on a LAN or Tailscale; the
  app says when a connection isn't encrypted. HTTPS with a certificate you installed on
  the phone works too.
- **Back** goes through the console's own history (closing its menu first), then to your
  servers. Links to other sites open in the browser. A redirect from HTTP to HTTPS on the
  same server stays in the app.
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

## Layout

```text
app/src/main/java/dev/agentproxy/app/
  ServersActivity.kt   the servers list: add (checked against /health), remove, reopen the last one, update notices
  ConsoleActivity.kt   one server's console in a WebView: sign-in cookie, Back, offline screen, external links, downloads
  Servers.kt           the stored servers, and checking an address is an agent-server (and its version)
  Insets.kt            keeping content clear of the system bars (Android 15 draws edge to edge)
app/src/main/res/      layouts, the console's colours (light and dark), the network security config
```
