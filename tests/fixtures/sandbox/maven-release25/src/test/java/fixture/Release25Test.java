package fixture;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class Release25Test {
  @Test
  void runsOnJdk25() {
    assertEquals(25, Runtime.version().feature());
    assertEquals("positive", Release25.describe(1));
  }
}
