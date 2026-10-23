package ossbot.fixture.core;

public final class Greeting {
    private Greeting() {}

    public static String of(String name) {
        return "Hello " + name;
    }
}
