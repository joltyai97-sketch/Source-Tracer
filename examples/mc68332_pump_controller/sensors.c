/*
 * sensors.c - Read analog inputs via the QSPI ADC and evaluate fault conditions.
 */
#include "regs.h"
#include "faults.h"

/* Latest scaled readings */
short          g_temp_c;
unsigned short g_vbus_mv;
unsigned short g_flow_lpm;   /* litres per minute x10 */
unsigned char  g_pump_on;

static unsigned short s_overtemp_ticks = 0;
static unsigned short s_dryrun_ticks = 0;

/* Read one ADC channel (0..7). Returns 12-bit raw value. */
unsigned short adc_read(unsigned char channel)
{
    QSPI_TR[0] = (unsigned short)(0x8000 | (channel << 12));
    SPCR1 |= 0x8000;                 /* SPE: start the queue */
    while (!(SPSR & SPSR_SPIF))
        ;                            /* wait for the transfer */
    SPSR &= ~SPSR_SPIF;
    return QSPI_RR[0] & 0x0FFF;
}

void sensors_sample(void)
{
    unsigned short raw;

    raw = adc_read(0);
    g_temp_c = (short)((raw * 165L) / 4095L) - 40;      /* -40..125 C */

    raw = adc_read(1);
    g_vbus_mv = (unsigned short)((raw * 40000L) / 4095L);

    raw = adc_read(2);
    g_flow_lpm = raw / 8;
}

/* Called every 10 ms from the PIT interrupt context. */
void sensors_check_faults(void)
{
    /* Sensor open circuit: temperature input pinned at the rail */
    if (g_temp_c >= 124) {
        set_fault(FAULT_SENSOR_OPEN, FLT_BIT_SENSOR);
        return;
    }

    /* Over-temperature with persistence and hysteresis */
    if (g_temp_c > TEMP_LIMIT_C) {
        if (s_overtemp_ticks < OVERTEMP_PERSIST_TICKS) {
            s_overtemp_ticks++;
        } else {
            set_fault(FAULT_OVERTEMP, FLT_BIT_OVERTEMP);
        }
    } else if (g_temp_c < TEMP_RECOVER_C) {
        s_overtemp_ticks = 0;
        clear_fault(FLT_BIT_OVERTEMP);
    }

    /* Supply voltage window */
    if (g_vbus_mv < VBUS_MIN_MV) {
        set_fault(FAULT_UNDERVOLT, FLT_BIT_VOLTAGE);
    } else if (g_vbus_mv > VBUS_MAX_MV) {
        set_fault(FAULT_OVERVOLT, FLT_BIT_VOLTAGE);
    } else {
        clear_fault(FLT_BIT_VOLTAGE);
    }

    /* Dry run: pump commanded on but no flow */
    if (g_pump_on && g_flow_lpm < 5) {
        if (++s_dryrun_ticks >= DRY_RUN_PERSIST_TICKS) {
            set_fault(FAULT_DRY_RUN, FLT_BIT_DRY_RUN);
            g_pump_on = 0;
        }
    } else {
        s_dryrun_ticks = 0;
    }
}
