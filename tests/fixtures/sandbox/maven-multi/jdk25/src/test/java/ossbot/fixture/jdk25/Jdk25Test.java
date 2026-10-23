package ossbot.fixture.jdk25;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import ossbot.fixture.core.Greeting;

class Jdk25Test {
    @Test
    void runsOnJdk25() {
        assertEquals(25, Runtime.version().feature());
        assertEquals("Hello 25", Greeting.of("25"));
    }
}
