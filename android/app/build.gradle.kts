import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// The release signing key lives outside the repo. Point JARVIS_KEYSTORE_PROPS at a keystore.properties file
// (storeFile, storePassword, keyAlias, keyPassword), or put one at android/keystore.properties (git-ignored).
// Every update must be signed with the same key, or Android refuses to install it over the old version.
val signing = Properties().apply {
    val path = System.getenv("JARVIS_KEYSTORE_PROPS") ?: rootProject.file("keystore.properties").path
    val f = file(path)
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "com.jarvis.homelab"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jarvis.homelab"
        minSdk = 26
        targetSdk = 35
        // Bump both for every release, and tag the GitHub release android-v<versionName>.
        versionCode = 11
        versionName = "2.0"
    }

    signingConfigs {
        if (signing.getProperty("storeFile") != null) {
            create("release") {
                storeFile = file(signing.getProperty("storeFile"))
                storePassword = signing.getProperty("storePassword")
                keyAlias = signing.getProperty("keyAlias")
                keyPassword = signing.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
}
