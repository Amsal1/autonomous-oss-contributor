plugins {
    java
}

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(22)
    }
}

tasks.register<JavaExec>("printJavaVersion") {
    javaLauncher.set(javaToolchains.launcherFor(java.toolchain))
    classpath = sourceSets["main"].runtimeClasspath
    mainClass.set("fixture.Main")
}
