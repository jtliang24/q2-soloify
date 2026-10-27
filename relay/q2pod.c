/* -q, for step 1's device pass: enough of librespot's contract (q2-pod docs/internals.md, Spotify)
 * that Q2 Pod, once Streaming's Spotify row is opened, powers the DAC on and holds standby and auto
 * power-off while the relay plays. The pid file makes .spotify/run exit at once, the control socket
 * answers spot_alive (its datagrams are read and dropped), and the state file says playing. Step 3
 * replaces this with the whole contract. SOLOIFY_Q2POD (default /tmp/q2-librespot) prefixes the
 * paths, for tests. */
#include "relay.h"

static int sock = -1;

static void put(const char *path, const char *text) {
    char tmp[160];
    int fd;
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    if ((fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644)) < 0) return;
    write(fd, text, strlen(text));
    close(fd);
    rename(tmp, path);
}

void q2pod_start(void) {
    const char *base = getenv("SOLOIFY_Q2POD");
    char path[128], text[32];
    struct sockaddr_un addr;
    if (!base || !*base) base = "/tmp/q2-librespot";
    snprintf(text, sizeof text, "%d\n", getpid());
    put(base, text);
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s.sock", base);
    unlink(addr.sun_path);
    if ((sock = socket(AF_UNIX, SOCK_DGRAM, 0)) < 0 || bind(sock, (struct sockaddr *)&addr, sizeof addr) < 0) {
        say("q2pod: can't bind %s", addr.sun_path);
        if (sock >= 0) close(sock), sock = -1;
    } else {
        fcntl(sock, F_SETFD, FD_CLOEXEC);
        fcntl(sock, F_SETFL, fcntl(sock, F_GETFL) | O_NONBLOCK);
    }
    snprintf(path, sizeof path, "%s.state", base);
    put(path, "state=playing\n");
    say("q2pod: %s says playing; open Streaming > Spotify once", path);
}

void q2pod_tick(void) {
    char buf[64];
    if (sock >= 0)
        while (read(sock, buf, sizeof buf) > 0) {}
}
