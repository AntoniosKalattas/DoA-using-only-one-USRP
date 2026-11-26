plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (GitHub Actions secrets), never from the repo:
//   ANDROID_KEYSTORE_PATH, ANDROID_KEYSTORE_PASSWORD, ANDROID_KEY_ALIAS, ANDROID_KEY_PASSWORD
// Without them, a release build falls back to the debug key so it still installs.
val ksPath: String? = System.getenv("ANDROID_KEYSTORE_PATH")
val hasReleaseKey = ksPath != null && file(ksPath).exists()

// CI passes the git tag (v1.5.0 -> "1.5.0") and the run number; local builds keep the defaults.
val ciVersionName: String? = (project.findProperty("appVersionName") as String?)?.removePrefix("v")
val ciVersionCode: Int? = (project.findProperty("appVersionCode") as String?)?.toIntOrNull()

android {
    namespace = "com.antonioskalattas.usrpbeacon"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.antonioskalattas.usrpbeacon"
        minSdk = 26          // AdvertisingSet API (Android 8.0+); Redmi Note 8 runs 9-11
        targetSdk = 35
        versionCode = ciVersionCode ?: 5
        versionName = ciVersionName ?: "1.4"
    }
    signingConfigs {
        create("release") {
            if (hasReleaseKey) {
                storeFile = file(ksPath!!)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (hasReleaseKey) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
// Only dependency: ARCore, for the "AR walk" calibration (the phone reports where it is while you
// walk around the antennas). Declared optional in the manifest, so phones without ARCore still
// install the app and fall back to the sweep calibration.
dependencies {
    implementation("com.google.ar:core:1.45.0")
}
