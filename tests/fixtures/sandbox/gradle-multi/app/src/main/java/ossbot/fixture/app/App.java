package ossbot.fixture.app;

import ossbot.fixture.lib.Greeting;
import org.apache.commons.lang3.StringUtils;

/** Compiled with the JDK 25 toolchain; depends on :lib and one Maven Central library. */
public final class App {
    private App() {}

    public static String greet(String name) {
        return StringUtils.capitalize(Greeting.of(name));
    }

    public static void main(String[] args) {
        System.out.println(greet("sandbox") + " on " + Runtime.version().feature());
    }
}
