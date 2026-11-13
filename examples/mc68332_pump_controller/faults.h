/*
 * faults.h - Diagnostic fault codes reported over the SCI status frame.
 */
#ifndef FAULTS_H
#define FAULTS_H

/* Fault codes (sent as byte 3 of the status frame) */
typedef enum {
    FAULT_NONE        = 0x00,
    FAULT_OVERTEMP    = 0x12,
    FAULT_UNDERVOLT   = 0x21,
    FAULT_OVERVOLT    = 0x22,
    FAULT_DRY_RUN     = 0x30,
    FAULT_SENSOR_OPEN = 0x41,
    FAULT_COMMS       = 0x50
} fault_code_t;

/* Latched fault bits in g_fault_flags */
#define FLT_BIT_OVERTEMP   (1u << 0)
#define FLT_BIT_VOLTAGE    (1u << 1)
#define FLT_BIT_DRY_RUN    (1u << 2)
#define FLT_BIT_SENSOR     (1u << 3)

/* Persistence: a condition must hold this many 10 ms ticks before latching */
#define OVERTEMP_PERSIST_TICKS   50   /* 500 ms */
#define DRY_RUN_PERSIST_TICKS    300  /* 3 s */

#define TEMP_LIMIT_C        85
#define TEMP_RECOVER_C      75
#define VBUS_MIN_MV         20000
#define VBUS_MAX_MV         30000

extern unsigned short g_fault_flags;
extern fault_code_t   g_active_fault;

void set_fault(fault_code_t code, unsigned short bit);
void clear_fault(unsigned short bit);

#endif
