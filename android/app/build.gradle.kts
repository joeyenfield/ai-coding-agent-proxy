import java.util.Properties

// The project's one version (../VERSION), or the build's own from tools/dist (-PappVersion),
// which adds the commit: 0.4.0+56458f9. versionCode comes from its X.Y.Z.
val appVersion: String = (findProperty("appVersion") as String?)?.trim()?.takeIf { it.isNotEmpty() }
    ?: rootProject.file("../VERSION").takeIf { it.exists() }?.readText()?.trim()?.takeIf { it.isNotEmpty() }
    ?: "0.0.0"
val appVersionCode: Int = Regex("""^(\d+)\.(\d+)\.(\d+)""").find(appVersion)
    ?.destructured?.let { (major, minor, patch) -> major.toInt() * 10000 + minor.toInt() * 100 + patch.toInt() }
    ?.coerceAtLeast(1) ?: 1

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.agentproxy.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.agentproxy.app"
        // Android 10: DayNight platform themes and a WebView that updates through the Play Store.
        minSdk = 29
        targetSdk = 35
        versionCode = appVersionCode
        versionName = appVersion
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            // Sign with your own key: see README.md (keystore.properties).
            val keystore = rootProject.file("keystore.properties")
            if (keystore.exists()) {
                val properties = Properties().apply { keystore.inputStream().use { load(it) } }
                signingConfig = signingConfigs.create("release") {
                    storeFile = rootProject.file(properties.getProperty("storeFile"))
                    storePassword = properties.getProperty("storePassword")
                    keyAlias = properties.getProperty("keyAlias")
                    keyPassword = properties.getProperty("keyPassword")
                }
            }
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

// No libraries: the platform's own widgets and WebView, so the app stays small and has
// nothing to keep up to date but itself.
dependencies {}
