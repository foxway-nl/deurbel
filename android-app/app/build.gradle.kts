plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.google.services)
}

android {
    namespace = "nl.foxway.deurbel"
    compileSdk = 35

    defaultConfig {
        applicationId = "nl.foxway.deurbel"
        minSdk = 28
        targetSdk = 35
        versionCode = 31
        versionName = "3.4.1"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            isShrinkResources = false
            signingConfig = signingConfigs.getByName("debug")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            isDebuggable = true
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

    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    // ── AndroidX Core ────────────────────────────────────────────
    implementation(libs.core.ktx)
    implementation(libs.activity.compose)

    // ── Compose ──────────────────────────────────────────────────
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.compose.animation)
    debugImplementation(libs.compose.ui.tooling)

    // ── Lifecycle ────────────────────────────────────────────────
    implementation(libs.lifecycle.runtime.ktx)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.service)

    // ── Navigation ───────────────────────────────────────────────
    implementation(libs.navigation.compose)

    // ── Network (Retrofit + OkHttp) ─────────────────────────────
    implementation(libs.retrofit)
    implementation(libs.retrofit.gson)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)
    implementation(libs.gson)

    // ── DataStore ────────────────────────────────────────────────
    implementation(libs.datastore.preferences)

    // ── Firebase ─────────────────────────────────────────────────
    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging)

    // ── Coroutines ───────────────────────────────────────────────
    implementation(libs.coroutines.core)
    implementation(libs.coroutines.android)

    // ── WebRTC (Stream) ──────────────────────────────────────────
    implementation(libs.stream.webrtc)

    // ── Media3 / ExoPlayer ───────────────────────────────────────
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.ui)

    // ── Image Loading ────────────────────────────────────────────
    implementation(libs.coil.compose)

    // ── Permissions ──────────────────────────────────────────────
    implementation(libs.accompanist.permissions)

    // ── QR & Barcode Scanning ─────────────────────────────────────
    implementation(libs.zxing.core)

    // ── CameraX ───────────────────────────────────────────────────
    implementation(libs.camera.core)
    implementation(libs.camera.camera2)
    implementation(libs.camera.lifecycle)
    implementation(libs.camera.view)
}
