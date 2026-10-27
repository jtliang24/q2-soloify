/* The sink, as librespot's subprocess backend has it: the stock ffmpeg decoding the relay's Ogg into
 * /bin/sh ./aplay.sh, both in one process group so a kill stops them and aplay together. Both pipes
 * are cut to a page, so audio waits in the relay's ring, where it's counted, not in a pipe. */
#include "relay.h"

#define DECODE "/usr/bin/ffmpeg -nostdin -loglevel error -probesize 32 -analyzeduration 0 -fflags nobuffer " \
               "-f ogg -i pipe:0 -ac 2 -ar 44100 -f s16le pipe:1"
#define SINK "/bin/sh ./aplay.sh"
#define PIPE_BYTES 4096

static int group = -1, decoder = -1, player = -1;

static const char *env_or(const char *name, const char *dflt) {
    const char *v = getenv(name);
    return v && *v ? v : dflt;
}

static int spawn(const char *cmd, int in, int out, int pgid, int *close_fds, int n) {
    int pid = fork();
    if (pid) {
        if (pid > 0) setpgid(pid, pgid ? pgid : pid);
        return pid;
    }
    setpgid(0, pgid);
    dup2(in, 0);
    if (out >= 0) dup2(out, 1);
    for (int i = 0; i < n; ++i) close(close_fds[i]);
    execl(env_or("SOLOIFY_SH", "/bin/sh"), "sh", "-c", cmd, (char *)NULL);
    _exit(127);
}

int sink_start(void) {
    int a[2], b[2];
    if (pipe(a) < 0) return -1;
    if (pipe(b) < 0) {
        close(a[0]), close(a[1]);
        return -1;
    }
    fcntl(a[1], F_SETPIPE_SZ, PIPE_BYTES);
    fcntl(b[1], F_SETPIPE_SZ, PIPE_BYTES);
    int fds[4] = {a[0], a[1], b[0], b[1]};
    decoder = spawn(env_or("SOLOIFY_DECODE", DECODE), a[0], b[1], 0, fds, 4);
    group = decoder;
    player = decoder > 0 ? spawn(env_or("SOLOIFY_SINK", SINK), b[0], -1, group, fds, 4) : -1;
    close(a[0]), close(b[0]), close(b[1]);
    if (decoder < 0 || player < 0) {
        close(a[1]);
        sink_stop();
        return -1;
    }
    fcntl(a[1], F_SETFD, FD_CLOEXEC);
    fcntl(a[1], F_SETFL, fcntl(a[1], F_GETFL) | O_NONBLOCK);
    return a[1];
}

void sink_stop(void) {
    if (group > 0) kill(-group, SIGKILL);
    if (decoder > 0) waitpid(decoder, NULL, 0);
    if (player > 0) waitpid(player, NULL, 0);
    group = decoder = player = -1;
}

int sink_exited(void) {
    return (decoder > 0 && waitpid(decoder, NULL, WNOHANG) == decoder) ||
           (player > 0 && waitpid(player, NULL, WNOHANG) == player);
}
