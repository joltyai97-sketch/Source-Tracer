/*
 * main.c - Pump controller for MC68332 (Introl-style C).
 */
#include "regs.h"
#include "faults.h"

unsigned short g_fault_flags = 0;
fault_code_t   g_active_fault = FAULT_NONE;
volatile unsigned long g_ticks = 0;     /* 10 ms system ticks */

extern unsigned char g_pump_on;

void sensors_sample(void);
void sensors_check_faults(void);
void comms_send_status(void);
void sci_init(void);
void pump_set(unsigned char on);
void cpu_idle(void);                   /* implemented in startup.s */

void set_fault(fault_code_t code, unsigned short bit)
{
    g_fault_flags |= bit;
    if (g_active_fault == FAULT_NONE)
        g_active_fault = code;          /* first fault wins */
}

void clear_fault(unsigned short bit)
{
    g_fault_flags &= ~bit;
    if (g_fault_flags == 0)
        g_active_fault = FAULT_NONE;
}

void pump_set(unsigned char on)
{
    if (g_fault_flags & (FLT_BIT_OVERTEMP | FLT_BIT_DRY_RUN))
        on = 0;                         /* interlock */
    g_pump_on = on;
    if (on)
        PORTE |= 0x01;
    else
        PORTE &= ~0x01;
}

static void watchdog_service(void)
{
    SWSR = 0x55;
    SWSR = 0xAA;
}

/* Periodic interrupt timer: 10 ms */
interrupt void pit_isr(void)
{
    g_ticks++;
    sensors_sample();
    sensors_check_faults();
}

void main(void)
{
    SYPCR = 0x80 | 0x40;                /* watchdog enabled */
    SYNCR = 0x7F08;
    DDRE  = 0x01;
    PICR  = 0x0640;                     /* level 6, vector 0x40 */
    PITR  = 0x0052;                     /* ~10 ms */
    sci_init();

    pump_set(1);

    for (;;) {
        if ((g_ticks % 10) == 0)        /* every 100 ms */
            comms_send_status();
        watchdog_service();
        cpu_idle();
    }
}
