# shellcheck shell=bash
# JVM setup for the code-running entrypoints (DESIGN §9.6, §9.8, §9.9, §10.1; ADR-0005 addendum
# 2026-10-23 "JVM sandbox"). Sourced by fetch.sh, validate.sh and exec.sh; not a job entrypoint.
#
# ossbot_jvm_setup <online|validate|offline>   (needs WORK, HOME=$WORK/.home, GRADLE_USER_HOME set by the caller)
#   online   (fetch, networked agent-exec): Gradle online,  Maven online,  proxy everywhere
#   validate (validate):                   Gradle offline, Maven online,  proxy everywhere (V-M6)
#   offline  (offline agent-exec):         Gradle offline, Maven offline, no proxy needed (no network)
#
# The JVM ignores HTTPS_PROXY, so the proxy goes into $GRADLE_USER_HOME/gradle.properties and a Maven
# user settings.xml. Gradle offline mode comes from an init script (there is no offline Gradle property;
# GRADLE_OPTS=--offline is not a JVM option and kills the JVM). Every file is written fresh at every
# job start (rm -f, then a new file, 0644): a symlink in its place is removed, never followed. Repo code
# can write /work/cache in fetch; that gives it nothing its build scripts don't already have in validate.
# A missing or malformed HTTPS_PROXY writes no proxy lines (one stderr line) and never fails the job.

PROXY_RE='^http://([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?):([0-9]{1,5})/?$'
JDKS=/usr/lib/jvm/java-17-openjdk-amd64,/usr/lib/jvm/java-21-openjdk-amd64,/usr/lib/jvm/java-25-openjdk-amd64
OSSBOT_GRADLE_DIR="${OSSBOT_GRADLE_DIR:-/opt/ossbot/gradle}"  # the baked init scripts (tests relocate it)

ossbot_jvm_setup() {
  local mode="${1:-}" host="" port="" offline=false
  case "$mode" in
    online | validate | offline) ;;
    *)
      echo "jvm_env: unknown mode '${mode}'" >&2
      return 2
      ;;
  esac
  if [[ -z "${WORK:-}" || -z "${HOME:-}" || -z "${GRADLE_USER_HOME:-}" ]]; then
    echo "jvm_env: WORK, HOME and GRADLE_USER_HOME must be set" >&2
    return 2
  fi
  if [[ "$mode" != offline ]]; then
    if [[ "${HTTPS_PROXY:-}" =~ $PROXY_RE ]]; then
      host="${BASH_REMATCH[1]}"
      port="${BASH_REMATCH[3]}"
      if ((10#$port < 1 || 10#$port > 65535)); then
        host=""
        port=""
      else
        port="$((10#$port))"
      fi
    fi
    if [[ -z "$host" ]]; then
      echo "jvm_env: no usable HTTPS_PROXY; JVM tools get no proxy" >&2
    fi
  else
    offline=true
  fi

  mkdir -p "$GRADLE_USER_HOME/init.d" "$HOME/.m2" "$WORK/cache/m2"

  # 3. gradle.properties: the proxy (V-G1; HTTPS uses http.nonProxyHosts), the JDK homes (V-G4).
  local gprops="$GRADLE_USER_HOME/gradle.properties"
  rm -f -- "$gprops"
  {
    echo "# written by ossbot (sandbox/entrypoints/lib/jvm_env.sh) at every job start; edits are lost"
    if [[ -n "$host" ]]; then
      printf 'systemProp.https.proxyHost=%s\nsystemProp.https.proxyPort=%s\n' "$host" "$port"
      printf 'systemProp.http.proxyHost=%s\nsystemProp.http.proxyPort=%s\n' "$host" "$port"
      echo 'systemProp.http.nonProxyHosts=localhost|127.*|[::1]'
    fi
    echo "org.gradle.java.installations.paths=$JDKS"
    if [[ "$mode" != online ]]; then
      echo "org.gradle.java.installations.auto-download=false"
    fi
  } >"$gprops"
  chmod 0644 "$gprops"

  # 4. Gradle offline mode (V-G2, V-G3): the init script in validate and offline, none online.
  local init="$GRADLE_USER_HOME/init.d/ossbot-offline.init.gradle"
  rm -f -- "$init"
  if [[ "$mode" != online ]]; then
    if cp -- "$OSSBOT_GRADLE_DIR/ossbot-offline.init.gradle" "$init"; then
      chmod 0644 "$init"
    else
      echo "jvm_env: offline init script missing; Gradle runs without --offline" >&2
    fi
  fi

  # 5. Maven user settings (V-M2): local repository on the task volume, offline only in agent-exec.
  local settings="$HOME/.m2/settings.xml"
  rm -f -- "$settings"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!-- written by ossbot (sandbox/entrypoints/lib/jvm_env.sh) at every job start; edits are lost -->'
    echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">'
    printf '  <localRepository>%s</localRepository>\n' "$WORK/cache/m2"
    echo '  <interactiveMode>false</interactiveMode>'
    printf '  <offline>%s</offline>\n' "$offline"
    if [[ -n "$host" ]]; then
      echo '  <proxies>'
      local proto
      for proto in http https; do
        printf '    <proxy><id>ossbot-%s</id><active>true</active><protocol>%s</protocol>' "$proto" "$proto"
        printf '<host>%s</host><port>%s</port><nonProxyHosts>localhost|127.*|[::1]</nonProxyHosts></proxy>\n' \
          "$host" "$port"
      done
      echo '  </proxies>'
    fi
    echo '</settings>'
  } >"$settings"
  chmod 0644 "$settings"

  # 5a. Maven toolchains (maven-toolchains-plugin): the same three JDKs, in every mode.
  local toolchains="$HOME/.m2/toolchains.xml" v
  rm -f -- "$toolchains"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!-- written by ossbot (sandbox/entrypoints/lib/jvm_env.sh) at every job start; edits are lost -->'
    echo '<toolchains xmlns="http://maven.apache.org/TOOLCHAINS/1.1.0">'
    for v in 17 21 25; do
      printf '  <toolchain><type>jdk</type><provides><version>%s</version><vendor>openjdk</vendor></provides>' "$v"
      printf '<configuration><jdkHome>/usr/lib/jvm/java-%s-openjdk-amd64</jdkHome></configuration></toolchain>\n' "$v"
    done
    echo '</toolchains>'
  } >"$toolchains"
  chmod 0644 "$toolchains"

  # 6. Environment: JDK 21 is the default; Maven finds its settings through user.home (V-M2, V-M3); bin-type
  # Maven wrappers download through the JVM proxy properties (V-M5). JAVA_TOOL_OPTIONS prints "Picked up".
  local mopts="-Duser.home=$HOME"
  if [[ -n "$host" ]]; then
    mopts+=" -Dhttps.proxyHost=$host -Dhttps.proxyPort=$port -Dhttp.proxyHost=$host -Dhttp.proxyPort=$port"
  fi
  export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 MAVEN_USER_HOME="$HOME/.m2" MAVEN_OPTS="$mopts"
  unset GRADLE_OPTS JAVA_TOOL_OPTIONS
  return 0
}
