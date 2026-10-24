plugins {
    java
}

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(providers.gradleProperty("jdkVersion").get().toInt())
    }
}

tasks.register<JavaExec>("printJavaVersion") {
    javaLauncher.set(javaToolchains.launcherFor(java.toolchain))
    classpath = sourceSets["main"].runtimeClasspath
    mainClass.set("fixture.Main")
}
