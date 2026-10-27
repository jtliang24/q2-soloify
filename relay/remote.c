/* .soloify/remote, as `soloify remote` prints it: one endpoint a line, tried in order. */
#include "relay.h"

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Private and loopback addresses and .local names are the LAN: a short connect timeout, a short
 * buffer */
static int is_lan(const char *h) {
    int a = 0, b = 0, n = 0;
    const char *p = h;
    size_t len = strlen(h);
    if (len > 6 && !strcmp(h + len - 6, ".local")) return 1;
    for (; *p >= '0' && *p <= '9'; ++p) a = a * 10 + *p - '0', ++n;
    if (!n || *p++ != '.') return 0;
    for (n = 0; *p >= '0' && *p <= '9'; ++p) b = b * 10 + *p - '0', ++n;
    if (!n || *p != '.') return 0;
    return a == 10 || a == 127 || (a == 172 && b >= 16 && b < 32) || (a == 192 && b == 168);
}

static int word(char **s, char *out, int cap) {
    int n = 0;
    while (**s == ' ' || **s == '\t') ++*s;
    while (**s && **s != ' ' && **s != '\t') {
        if (n == cap - 1) return -1;
        out[n++] = *(*s)++;
    }
    out[n] = 0;
    return n;
}

static int parse(char *line, struct endpoint *ep) {
    char opt[96];
    int n;
    memset(ep, 0, sizeof *ep);
    if (word(&line, ep->host, sizeof ep->host) <= 0 || word(&line, ep->port, sizeof ep->port) <= 0 ||
        word(&line, ep->token, sizeof ep->token) <= 0)
        return -1;
    while ((n = word(&line, opt, sizeof opt)) > 0) {
        if (!strncmp(opt, "sha256=", 7) && n == 7 + 64) {
            for (int i = 0; i < 32; ++i) {
                int hi = hexval(opt[7 + 2 * i]), lo = hexval(opt[8 + 2 * i]);
                if (hi < 0 || lo < 0) return -1;
                ep->pin[i] = hi << 4 | lo;
            }
            ep->pinned = 1;
        } else if (!strncmp(opt, "buffer=", 7)) {
            ep->buffer_ms = strtol(opt + 7, NULL, 10);
            if (ep->buffer_ms < 200 || ep->buffer_ms > 20000) return -1;
        } else {
            return -1;
        }
    }
    if (n < 0) return -1;
    ep->lan = is_lan(ep->host);
    return 0;
}

int remote_load(const char *path, struct endpoint *eps, int max) {
    char buf[4096], *line, *end;
    int fd = open(path, 0), len = 0, r, count = 0, no = 0;
    if (fd < 0) return -1;
    while (len < (int)sizeof buf - 1 && (r = read(fd, buf + len, sizeof buf - 1 - len)) > 0) len += r;
    close(fd);
    buf[len] = 0;
    for (line = buf; *line && count < max; line = end) {
        end = strchr(line, '\n');
        end = end ? (*end = 0, end + 1) : line + strlen(line);
        ++no;
        len = strlen(line);
        if (len && line[len - 1] == '\r') line[len - 1] = 0;
        while (*line == ' ' || *line == '\t') ++line;
        if (!*line || *line == '#') continue;
        if (parse(line, &eps[count]) < 0) say("remote: line %d unreadable, skipped", no);
        else ++count;
    }
    return count;
}
