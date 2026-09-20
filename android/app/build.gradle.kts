// import 必须在最前面；而且不能用 java.text.XXX 全限定名——
// Kotlin DSL 脚本里 `java` 被 Gradle 的 java 扩展占了名字，会报 Unresolved reference
import java.text.SimpleDateFormat
import java.util.Date

plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "com.xiaoyin.remote"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.xiaoyin.remote"
        minSdk = 27
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        // 界面上显示构建时间：这一版要反复装很多次，
        // 没有它就只能靠"文件大小差几 KB"猜装没装对（真这么猜过）
        val buildStamp = SimpleDateFormat("MM-dd HH:mm").format(Date())
        buildConfigField("String", "BUILD_TIME", "\"$buildStamp\"")

        // 只留手机自己的架构：sherpa-onnx 的 AAR 里带了 4 个 ABI 的原生库，
        // 全打进去白背三份（arm64 一份就 31.9MB）
        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    packaging {
        jniLibs {
            // AAR 自带的原生库和别的依赖撞名时以先到的为准（本来就不该有第二个）
            pickFirsts += listOf("**/libonnxruntime.so", "**/libsherpa-onnx-jni.so")
        }
    }

    compileOptions {
        // Java 17：sherpa-onnx 的 AAR 是 JDK 17 编的（class 文件版本 61），
        // 用 11 编译会报 "class file has wrong version 61.0, should be 55.0"。
        // 构建本来就要求 JDK 17，所以升上来对现有代码零影响
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    implementation(libs.okhttp)

    // 端侧唤醒/识别/合成：官方没有 Maven 发布，用本地 AAR（见 android/fetch_deps.py）。
    // ⚠️ 绝对不要再引 onnxruntime-android —— AAR 里已经带了编译好的 libonnxruntime.so，
    //    再引一个别的版本就是 UnsatisfiedLinkError: OrtGetApiBase 的成因
    implementation(files("libs/sherpa-onnx-1.13.8.aar"))
    // AAR 是 Kotlin 编译产物（javap 显示 Compiled from "KeywordSpotter.kt"），
    // 运行时需要 kotlin-stdlib，否则 NoClassDefFoundError: kotlin/jvm/internal/Intrinsics
    implementation("org.jetbrains.kotlin:kotlin-stdlib:2.0.21")
}
