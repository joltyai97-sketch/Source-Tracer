#ifndef _ST_STDLIB_H
#define _ST_STDLIB_H
#include <stddef.h>
int  abs(int x);
long labs(long x);
void *malloc(size_t n);
void free(void *p);
int  atoi(const char *s);
void exit(int code);
void abort(void);
#endif
