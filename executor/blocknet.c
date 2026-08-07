/* blocknet.c — LD_PRELOAD shim that blocks network egress for sandboxed code.
 *
 * Overrides socket()/connect() to fail with EACCES. Deliberately does NOT
 * override socketpair(): runtimes (tokio, libuv, glibc) use it for internal
 * signal/event plumbing, and blocking it crashes the host shim itself.
 * Best-effort: only affects dynamically-linked programs; statically-linked
 * binaries ignore LD_PRELOAD entirely.
 *
 * Build: gcc -shared -fPIC -O2 -o blocknet.so blocknet.c
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/types.h>

int socket(int domain, int type, int protocol)
{
    (void)domain;
    (void)type;
    (void)protocol;
    errno = EACCES;
    return -1;
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    (void)fd;
    (void)addr;
    (void)len;
    errno = EACCES;
    return -1;
}
