/* The relay: dials the bridge over TLS and plays its Opus through the stock ffmpeg into aplay.sh
 * (docs/wire.md). One process, one poll loop. */
#ifndef RELAY_H
#define RELAY_H
#include "sys.h"

#define FRAME_MS 20
#define MAX_ENDPOINTS 8

extern int verbose;
extern volatile int quitting, retry_remote;
int relay_send(char type, const void *payload, int n);
void say(const char *fmt, ...);
uint64_t now_ms(void);
/* Waits for events on fd until deadline (ms, now_ms's clock), feeding the sink meanwhile: the
 * revents, 0 at the deadline. fd < 0 just waits. */
int relay_wait(int fd, short events, uint64_t deadline);

/* remote.c: .soloify/remote, one endpoint a line: HOST PORT TOKEN [sha256=HEX] [buffer=MS] */
struct endpoint {
    char host[128], port[8], token[80];
    unsigned char pin[32];
    int pinned, lan, buffer_ms;
    int unchecked; /* pairing: no pin and no chain check, the certificate's hash kept in the link */
};
int remote_load(const char *path, struct endpoint *eps, int max);
/* One remote line (changed in place) into ep, as remote_load reads it: 0, or -1 when unreadable */
int remote_parse(char *line, struct endpoint *ep);
/* The integer in .soloify/NAME, a setting the Q2 Pod firmware writes; 0 if absent or invalid */
int remote_setting(const char *remote, const char *name);

/* tls.c */
struct link {
    int fd;
    SSL_CTX *ctx;
    SSL *ssl;
    unsigned char peer[32]; /* the leaf certificate's DER SHA-256, after an unchecked open */
};
int link_open(struct link *l, const struct endpoint *ep);
int link_read(struct link *l, void *buf, int n);        /* >0 bytes, 0 none yet, -1 gone */
int link_write(struct link *l, const void *buf, int n); /* bytes taken (maybe 0), -1 gone */
void link_close(struct link *l);

/* wire.c: frames of a type byte and a big-endian u32 length */
struct wire {
    unsigned char in[(1 << 20) + 5], out[32768];
    int nin, nout;
};
int wire_send(struct wire *w, char type, const void *payload, int n); /* -1: no room */
/* The next whole frame in w->in: its type, with *payload and *n set; 0 when none yet, -1 if too
 * big. wire_consume drops it once handled. */
int wire_next(struct wire *w, const unsigned char **payload, int *n);
void wire_consume(struct wire *w);
uint32_t be32(const unsigned char *p);
void put_be32(unsigned char *p, uint32_t v);

/* ogg.c: one page per packet; out needs 27 + 6 + n bytes for a packet, 128 for the headers */
struct ogg { uint32_t seq; uint64_t granule; };
void ogg_init(void);
int ogg_headers(struct ogg *o, unsigned char *out, unsigned skip);
int ogg_packet(struct ogg *o, unsigned char *out, const unsigned char *p, int n, unsigned samples, int end);

/* sink.c: the decoder and aplay.sh; sink_start gives the pipe to write the Ogg into */
int sink_start(void);
int sink_rendered(uint64_t *frames, unsigned *rate);
int sink_next(int fd); /* drain/reap only the decoder, retaining the PCM player */
void sink_finish(int fd);
int sink_player(void);
void sink_stop(void);
int sink_exited(void);

/* q2pod.c: -q, librespot's state, controls and the minimal local browser */
void q2pod_start(void);
void q2pod_tick(void);
void q2pod_stop(void);
void q2pod_config(int valid);
void q2pod_link(int up);
void q2pod_frame(int type, const unsigned char *p, int n);
void q2pod_started(void); /* the first buffered packet is about to reach the decoder */
void q2pod_paused(void); /* stop/reap before publishing the frozen bookmark */
int q2pod_output_lost(void); /* 1 when Q2 Pod paused for a lost output */
int q2pod_route(char *out, int cap); /* the selected output device; 0 without Q2 Pod */
void q2pod_capture(void); /* a fresh encoder begins after ordered control boundaries */
int q2pod_wants(void);
int q2pod_audio(void);
int q2pod_ready(void);
/* --manage's page (manage.c): only the browse socket, at SOLOIFY_Q2POD or fallback; 0 when another
 * process owns it */
int q2pod_serve(const char *fallback);
uint64_t q2pod_last_request(void); /* when the last browse connection came */
int q2pod_clients(void);           /* browse connections still open */

/* manage.c: q2-soloify --manage, the Soloify app's page on upstream Q2 Pod */
int manage_main(void);
/* The reply to one well-framed request (raw, n bytes, id already checked) in out: its length */
int manage_reply(const char *id, const char *raw, int n, char *out, int cap);
/* swap.c: the relay into the Spotify slot (../.spotify) and out again. swap_rows: which of the
 * manager's swap rows to show, given whether the slot runs our relay (ours) and an older copy of it
 * (older). swap_in and swap_out: 1 when done, 0 not; msg says what happened, for the page. */
enum { SWAP_USE = 1, SWAP_UPDATE = 2, SWAP_REPAIR = 4, SWAP_RESTORE = 8 };
int swap_rows(int ours, int older);
int swap_in(char *msg, int cap);
int swap_out(char *msg, int cap);
/* main.c: dial ep and say hello, then hang up; 0 when the bridge answered ok */
int relay_probe(const struct endpoint *ep);
/* main.c: dial ep and say ping, which leaves a playing relay its session: 0 on pong, 1 when the
 * bridge closed without a word after the handshake (a bridge without ping, or a wrong token), -1
 * when it wasn't reached */
int relay_ping(const struct endpoint *ep);

/* pair.c: pairing by code (docs/wire.md, Pairing). pair_run looks for the bridge (host NULL) or
 * dials host:port, shows the code (pair.code) and waits for the answer, serving the page meanwhile
 * through relay_wait; on success it writes REMOTE, and the remote in the folder also too when not
 * NULL. Its outcome stays in pair.state until the next run; pair.cancel ends a run early. */
enum { PAIR_IDLE, PAIR_LOOKING, PAIR_CONNECTING, PAIR_CODE, PAIR_NOT_FOUND, PAIR_PAIRED, PAIR_DENIED,
       PAIR_FAILED };
struct pair {
    int state;
    volatile int cancel;
    char code[8];            /* "123 456", from PAIR_CODE on */
    char host[128], port[8]; /* the bridge dialled, or being dialled */
    char why[160];           /* PAIR_DENIED, PAIR_FAILED: in words */
    const char *watch;       /* a remote whose appearing cancels the run (the relay's own) */
    int wanted;              /* a run asked for: the relay page's Retry after an outcome */
    unsigned outcomes;       /* runs ended, so the page tells each outcome once */
};
extern struct pair pair;
void pair_run(const char *remote, const char *also, const char *host, const char *port);
int pair_waiting(void); /* a run under way: LOOKING, CONNECTING or CODE */
/* The relay page's error message while it has no remote */
void pair_message(char *out, int cap);
/* The code of F and the nonce, as docs/wire.md has it: "060 893" */
void pair_code(const unsigned char f[32], const unsigned char nonce[16], char out[8]);

/* pair.c, Away: whether the WAN line (any remote line after the first, the LAN one) has worked.
 * The relay appends "verified <unix time> <host>" to away beside its remote at a WAN line's first
 * connection, and counts WAN failures in /tmp/q2-soloify.away (SOLOIFY_AWAY_COUNT). */
enum { AWAY_NONE, AWAY_UNVERIFIED, AWAY_VERIFIED, AWAY_UNREACHED };
/* One round over the relay's n endpoints: up the index connected, -1 when every one failed */
void away_round(const char *remote, const struct endpoint *eps, int n, int up);
/* The state of remote's WAN line, from away in the folders dirs[0..n) and the count; *when its
 * verification's unix time */
int away_state(const char *remote, const char *const *dirs, int n, long long *when);

/* audio.c: the ring, the Ogg stream and the sink */
void audio_start(int target_ms); /* a link is up, with this buffer target */
void audio_packet(const unsigned char *p, int n, unsigned seq);
int audio_pollfd(struct pollfd *pfd); /* 1 with *pfd set when the sink wants waiting on */
void audio_service(short revents);
void audio_tick(void);
void audio_enable(int enabled); /* stop/reap and clear, or enable fresh capture */
void audio_pause(void);
int audio_bookmark(uint64_t *sample); /* hardware presentation sample clock, in ms */
const char *audio_clock_reason(void);
const char *sink_clock_reason(void);
int sink_unstarted(void); /* owned hardware seen, but not yet RUNNING */
int sink_established(void); /* this sink's hardware clock has run */
int sink_lost(void); /* that established substream has since disappeared */
void audio_capture(unsigned generation, unsigned skip, const unsigned char *primer, int n);
void audio_end(unsigned seq, unsigned samples);
void audio_clear(void); /* a seek/skip: stop and discard everything */
uint64_t audio_received(void);
uint64_t audio_played(void);
int audio_target(void);
int audio_stats(char *buf, int cap);
#endif
