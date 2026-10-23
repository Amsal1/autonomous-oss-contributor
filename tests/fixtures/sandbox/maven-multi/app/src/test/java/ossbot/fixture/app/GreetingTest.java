package ossbot.fixture.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import ossbot.fixture.core.Greeting;

class GreetingTest {
    @Test
    void greets() {
        assertEquals("Hello sandbox", Greeting.of("sandbox"));
    }
}
