/* The relay's view of libc and OpenSSL. RELAY_HOST builds against the host's headers, for tests.
 * The device build has no MIPS sysroot: as q2-pod's helpers do (patch/remote.c), it declares what
 * it uses from the stock rootfs's glibc 2.28 and OpenSSL 1.1.1f, with Linux's MIPS o32 constants. */
#ifndef RELAY_SYS_H
#define RELAY_SYS_H

#ifdef RELAY_HOST
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>
#define relay_errno errno
#else
#include <stdarg.h> /* the compiler's own, freestanding */
typedef unsigned size_t;
typedef int ssize_t;
typedef unsigned socklen_t;
typedef unsigned long nfds_t;
typedef unsigned char uint8_t;
typedef unsigned uint32_t;
typedef unsigned long long uint64_t;
#define NULL ((void *)0)

#define EINTR 4
#define EAGAIN 11
#define EINPROGRESS 150
#define O_WRONLY 1
#define O_APPEND 0x8
#define O_NONBLOCK 0x80
#define O_CREAT 0x100
#define F_SETFD 2
#define F_GETFL 3
#define F_SETFL 4
#define FD_CLOEXEC 1
#define F_SETPIPE_SZ 1031
#define AF_UNSPEC 0
#define SOCK_STREAM 2
#define SOL_SOCKET 0xffff
#define SO_KEEPALIVE 0x0008
#define SO_ERROR 0x1007
#define IPPROTO_TCP 6
#define TCP_NODELAY 1
#define TCP_KEEPIDLE 4
#define TCP_KEEPINTVL 5
#define TCP_KEEPCNT 6
#define POLLIN 0x1
#define POLLOUT 0x4
#define POLLERR 0x8
#define POLLHUP 0x10
#define SIGKILL 9
#define SIGPIPE 13
#define SIGTERM 15
#define SIGCHLD 18
#define SIG_IGN ((void (*)(int))1)
#define WNOHANG 1
#define CLOCK_MONOTONIC 1

struct timespec { long tv_sec, tv_nsec; };
struct pollfd { int fd; short events, revents; };
struct sockaddr;
struct addrinfo {
    int ai_flags, ai_family, ai_socktype, ai_protocol;
    socklen_t ai_addrlen;
    struct sockaddr *ai_addr;
    char *ai_canonname;
    struct addrinfo *ai_next;
};

int *__errno_location(void);
#define relay_errno (*__errno_location())
int open(const char *, int, ...), close(int), dup2(int, int), pipe(int *), fcntl(int, int, ...);
ssize_t read(int, void *, size_t), write(int, const void *, size_t);
int fork(void), setpgid(int, int), kill(int, int), waitpid(int, int *, int), execl(const char *, const char *, ...);
__attribute__((noreturn)) void _exit(int);
int poll(struct pollfd *, nfds_t, int), clock_gettime(int, struct timespec *);
void (*signal(int, void (*)(int)))(int);
int socket(int, int, int), connect(int, const struct sockaddr *, socklen_t);
int setsockopt(int, int, int, const void *, socklen_t), getsockopt(int, int, int, void *, socklen_t *);
int getaddrinfo(const char *, const char *, const struct addrinfo *, struct addrinfo **);
void freeaddrinfo(struct addrinfo *);
void *malloc(size_t), free(void *);
void *memcpy(void *, const void *, size_t), *memmove(void *, const void *, size_t), *memset(void *, int, size_t);
void *memchr(const void *, int, size_t);
int memcmp(const void *, const void *, size_t), strcmp(const char *, const char *), strncmp(const char *, const char *, size_t);
size_t strlen(const char *);
char *getenv(const char *), *strchr(const char *, int);
long strtol(const char *, char **, int);
int snprintf(char *, size_t, const char *, ...), vsnprintf(char *, size_t, const char *, va_list);

/* OpenSSL 1.1.1f */
typedef struct ssl_st SSL;
typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_method_st SSL_METHOD;
typedef struct x509_st X509;
typedef struct evp_md_st EVP_MD;
#define SSL_VERIFY_NONE 0
#define SSL_VERIFY_PEER 1
#define SSL_ERROR_WANT_READ 2
#define SSL_ERROR_WANT_WRITE 3
#define SSL_ERROR_ZERO_RETURN 6
#define X509_V_OK 0
#define TLS1_2_VERSION 0x0303
#define SSL_MODE_ENABLE_PARTIAL_WRITE 0x1
#define SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER 0x2
#define SSL_CTRL_MODE 33
#define SSL_CTRL_SET_TLSEXT_HOSTNAME 55
#define SSL_CTRL_SET_MIN_PROTO_VERSION 123
#define TLSEXT_NAMETYPE_host_name 0
#define EVP_MAX_MD_SIZE 64
const SSL_METHOD *TLS_client_method(void);
SSL_CTX *SSL_CTX_new(const SSL_METHOD *);
void SSL_CTX_free(SSL_CTX *);
long SSL_CTX_ctrl(SSL_CTX *, int, long, void *);
void SSL_CTX_set_verify(SSL_CTX *, int, void *);
int SSL_CTX_load_verify_locations(SSL_CTX *, const char *, const char *);
SSL *SSL_new(SSL_CTX *);
void SSL_free(SSL *);
int SSL_set_fd(SSL *, int), SSL_set1_host(SSL *, const char *), SSL_connect(SSL *);
long SSL_ctrl(SSL *, int, long, void *);
int SSL_read(SSL *, void *, int), SSL_write(SSL *, const void *, int);
int SSL_get_error(const SSL *, int), SSL_pending(const SSL *), SSL_shutdown(SSL *);
long SSL_get_verify_result(const SSL *);
X509 *SSL_get_peer_certificate(const SSL *);
void X509_free(X509 *);
const EVP_MD *EVP_sha256(void);
int X509_digest(const X509 *, const EVP_MD *, unsigned char *, unsigned *);
void ERR_clear_error(void);
#define SSL_CTX_set_min_proto_version(c, v) SSL_CTX_ctrl(c, SSL_CTRL_SET_MIN_PROTO_VERSION, v, NULL)
#define SSL_CTX_set_mode(c, m) SSL_CTX_ctrl(c, SSL_CTRL_MODE, m, NULL)
#define SSL_set_tlsext_host_name(s, n) SSL_ctrl(s, SSL_CTRL_SET_TLSEXT_HOSTNAME, TLSEXT_NAMETYPE_host_name, (void *)(n))
#endif

#endif
