java {
  toolchain {
    languageVersion = JavaLanguageVersion.of(providers.gradleProperty("jdkVersion").get().toInt())
  }
}
