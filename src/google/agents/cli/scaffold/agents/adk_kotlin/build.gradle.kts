// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version "2.3.20"
    // Generates `generatedTools()` from @Tool methods.
    id("com.google.devtools.ksp") version "2.3.9"
    // Code style checks: `./gradlew ktlintCheck`, or `./gradlew ktlintFormat` to fix.
    id("org.jlleitschuh.gradle.ktlint") version "14.2.0"
    application
}

repositories {
    mavenCentral()
}

val adkVersion = "1.4.0"
// The A2A Java SDK and Ktor versions the ADK Kotlin A2A module and web server are built on.
val a2aVersion = "1.3.2.Final"
val ktorVersion = "3.6.0"

dependencies {
    implementation("com.google.adk:google-adk-kotlin-core:$adkVersion")
    implementation("com.google.adk:google-adk-kotlin-webserver:$adkVersion")
    ksp("com.google.adk:google-adk-kotlin-processor:$adkVersion")
    // Serves A2A next to the ADK API; see A2aServer.kt.
    implementation("org.a2aproject.sdk:a2a-java-sdk-server-common:$a2aVersion")
    implementation("org.a2aproject.sdk:a2a-java-sdk-transport-jsonrpc:$a2aVersion")
    // The ADK web server's engine, for the A2A routes.
    implementation("io.ktor:ktor-server-core:$ktorVersion")
    testImplementation(kotlin("test"))
    // Gradle 9 no longer puts the JUnit Platform launcher on the test classpath.
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
}

// Target Java 17 from whichever JDK 17+ runs Gradle, rather than requiring an
// installed JDK 17 toolchain.
tasks.withType<JavaCompile>().configureEach {
    options.release = 17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
        freeCompilerArgs.add("-Xjdk-release=17")
    }
}

application {
    mainClass.set("{{cookiecutter.java_package}}.MainKt")
}

ktlint {
    // KSP adds its generated sources to the main source set; lint only hand-written code.
    filter {
        exclude { it.file.startsWith(layout.buildDirectory.get().asFile) }
    }
}
