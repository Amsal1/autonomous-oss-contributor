package ossbot.fixture.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class AppTest {
    @Test
    void greets() {
        assertEquals("Hello sandbox", App.greet("sandbox"));
    }

    @Test
    void runsOnTheJdk25Toolchain() {
        assertEquals(25, Runtime.version().feature());
    }
}
