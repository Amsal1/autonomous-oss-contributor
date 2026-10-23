package ossbot.fixture.lib;

/** A class compiled with the JDK 17 toolchain. */
public final class Greeting {
    private Greeting() {}

    public static String of(String name) {
        return "hello " + name;
    }
}
