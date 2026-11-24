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

plugins {
    java
    application
    checkstyle
}

repositories {
    mavenCentral()
}

val adkVersion = "1.14.0"
// The A2A SDK version google-adk-a2a is built on; 0.3.4 breaks the JSON-RPC request binding.
val a2aVersion = "0.3.2.Final"
// The newest patch of the 4.0 line google-adk-dev is built on, for the Tomcat, Netty and Spring fixes it manages.
val springBootVersion = "4.0.10"

dependencies {
    // Keeps every Spring Boot module on the version google-adk-dev needs.
    implementation(platform("org.springframework.boot:spring-boot-dependencies:$springBootVersion"))
    implementation("com.google.adk:google-adk:$adkVersion")
    // Dev UI
    implementation("com.google.adk:google-adk-dev:$adkVersion")
    implementation("com.google.adk:google-adk-a2a:$adkVersion")
    // A2A JSON-RPC server
    implementation("io.github.a2asdk:a2a-java-sdk-transport-jsonrpc:$a2aVersion")
    // Pins for advisories the ADK and Spring Boot dependency sets still carry.
    constraints {
        // bcprov 1.82: GHSA-9pwp-9qqc-pr26 (critical) and GHSA-qp49-qgx5-5m26 (high), fixed in 1.85;
        // keep bcpkix/bcutil aligned on the same BouncyCastle release.
        implementation("org.bouncycastle:bcprov-jdk18on:1.85")
        implementation("org.bouncycastle:bcutil-jdk18on:1.85")
        implementation("org.bouncycastle:bcpkix-jdk18on:1.85")
        // guava 33.5.0-jre: GHSA-xxph-c9ww-hj94 (high), fixed in 33.7.2.
        implementation("com.google.guava:guava:33.7.2-jre")
        // httpcore5 5.4: GHSA-hf6x-8p5f-cgmf and GHSA-v3jc-474w-2wm6 (high), fixed in 5.4.3.
        implementation("org.apache.httpcomponents.core5:httpcore5:5.4.3")
        implementation("org.apache.httpcomponents.core5:httpcore5-h2:5.4.3")
    }
    testImplementation("org.springframework.boot:spring-boot-starter-test")
    // Gradle 9 no longer puts the JUnit Platform launcher on the test classpath.
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.withType<JavaCompile>().configureEach {
    options.release = 17
}

application {
    mainClass.set("{{cookiecutter.java_package}}.Main")
}

// Unit and e2e integration tests. Load tests need a running server, so they
// run only from the loadTest task.
tasks.test {
    useJUnitPlatform {
        excludeTags("load")
    }
}

// Usage: ./gradlew loadTest -Pstaging.url=http://127.0.0.1:8080 [-Pload.duration=30] [-Pload.users=10] [-Pload.ramp=2]
val loadTest by tasks.registering(Test::class) {
    description = "Runs the load tests against a running server."
    group = "verification"
    testClassesDirs = sourceSets.test.get().output.classesDirs
    classpath = sourceSets.test.get().runtimeClasspath
    useJUnitPlatform {
        includeTags("load")
    }
    testLogging {
        showStandardStreams = true
    }
    for (name in listOf("staging.url", "load.duration", "load.users", "load.ramp")) {
        providers.gradleProperty(name).orNull?.let { systemProperty(name, it) }
    }
    // A load test measures the server, so a rerun is never up to date.
    outputs.upToDateWhen { false }
}

checkstyle {
    // google_checks.xml ships inside the Checkstyle jar.
    config = resources.text.fromArchiveEntry(
        configurations.checkstyle.get().filter { it.name.startsWith("checkstyle-") },
        "google_checks.xml",
    )
}
