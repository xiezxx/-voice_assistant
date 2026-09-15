// 小音遥控 —— 安卓端工程设置
// 仓库全部走阿里云镜像（国内直连 maven.google.com / repo1.maven.org 经常超时）
pluginManagement {
    repositories {
        maven { url = uri("https://maven.aliyun.com/repository/public/") }
        maven { url = uri("https://maven.aliyun.com/repository/google/") }
        maven { url = uri("https://maven.aliyun.com/repository/gradle-plugin/") }
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        maven { url = uri("https://maven.aliyun.com/repository/public/") }
        maven { url = uri("https://maven.aliyun.com/repository/google/") }
        google()
        mavenCentral()
    }
}

rootProject.name = "XiaoyinRemote"
include(":app")
