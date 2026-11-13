/* Source Tracer stand-in <stdint.h>. Widths follow the target profile's
   char/short/int/long sizes via the __ST_* macros defined by the tool. */
#ifndef _ST_STDINT_H
#define _ST_STDINT_H
typedef signed char        int8_t;
typedef unsigned char      uint8_t;
typedef short              int16_t;
typedef unsigned short     uint16_t;
typedef __ST_INT32         int32_t;
typedef unsigned __ST_INT32 uint32_t;
typedef long long          int64_t;
typedef unsigned long long uint64_t;
typedef int8_t   int_least8_t;   typedef uint8_t  uint_least8_t;
typedef int16_t  int_least16_t;  typedef uint16_t uint_least16_t;
typedef int32_t  int_least32_t;  typedef uint32_t uint_least32_t;
typedef int8_t   int_fast8_t;    typedef uint8_t  uint_fast8_t;
typedef int16_t  int_fast16_t;   typedef uint16_t uint_fast16_t;
typedef int32_t  int_fast32_t;   typedef uint32_t uint_fast32_t;
typedef long          intptr_t;
typedef unsigned long uintptr_t;
typedef long long          intmax_t;
typedef unsigned long long uintmax_t;
#define INT8_MIN   (-128)
#define INT8_MAX   127
#define UINT8_MAX  255
#define INT16_MIN  (-32768)
#define INT16_MAX  32767
#define UINT16_MAX 65535
#define INT32_MIN  (-2147483647 - 1)
#define INT32_MAX  2147483647
#define UINT32_MAX 4294967295u
#endif
