package fixture;

public class Release25 {
  // a Java 25 language feature: the build fails on an older JDK (maven.compiler.release 25)
  public static String describe(Object o) {
    return switch (o) {
      case Integer i when i > 0 -> "positive";
      case Integer i -> "int";
      default -> "other";
    };
  }
}
