# Source Tracer — Project Plan

A desktop tool (Qt, dark mode) that loads microcontroller source code (C and C++,
including Arduino-style sketches), interprets it on the PC, lets the user set
inputs (function arguments, variables, pins, stubbed hardware calls) and shows
chosen variables as outputs.

## Goals and non-goals

**Primary users:** non-software engineers who need to *follow* and *predict*
the behaviour of existing, working firmware.

**Main use case:** answer two questions for a given scenario (a set of
inputs, possibly changing over time):
1. **Which fault codes are raised (and cleared), and when?**
2. **What data does the device send** (CAN frames, UART/Serial bytes, I2C/SPI
   writes, Modbus replies, ...) and what do those bytes mean?

Plus the reverse question: **"What has to happen for fault X to be raised?"**

**Goals**
- Load real-world firmware (C and a practical subset of C++) without editing it.
- Explain what the code does: step line by line, show which lines ran and why
  each value changed.
- "What if" analysis: set inputs, run, and read the outputs. Sweep one input
  across a range and plot the results.
- Never crash on code it doesn't understand. Degrade gracefully and say what
  was skipped.

**Non-goals**
- Cycle-accurate timing or exact register-level hardware emulation.
- A full C++ implementation (complex templates, the STL beyond basics, RTTI,
  exceptions).
- Being an IDE or a compiler. The code is assumed to already build and work.

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fast to build, good parsing and GUI libraries |
| GUI | PySide6 (Qt 6) with a custom dark palette | Desktop app, native look, solid widgets |
| Parser | `tree-sitter` + `tree-sitter-cpp` | Handles both C and C++, recovers from errors, gives exact line and column positions |
| Preprocessor | `pcpp` (pure-Python) + bundled fake headers | Expands `#define` / `#include` / `#if` while keeping line mapping |
| Execution | Tree-walking interpreter over the syntax tree | Every line, variable and call is visible |
| Memory model | **Object-based**, not byte-addressed | Simpler and good enough since exact accuracy isn't required. A pointer is a reference to (object, offset) |
| Plots | `pyqtgraph` | Fast, Qt-native |
| Code view | `QScintilla` (or QPlainTextEdit + highlighter) | Line highlighting, gutter markers |

## Target-agnostic design, MC68332 first

The tool must work with **any microcontroller, any protocol and any
fault-code convention**. Nothing about a particular chip, bus or fault scheme
is hard-coded. Instead:

- **Target profiles** (`source_tracer/profiles/*.yaml`) describe a chip:
  - endianness and type sizes (`int` is 16-bit on some 68k compilers);
  - compiler-specific keywords to ignore (`interrupt`, `@far`, `__near`, ...);
  - the memory-mapped register map: name, address, width, module,
    description, and a *role*:
    - `tx` — a write sends data on a named channel;
    - `rx` — a read takes from a scripted input queue;
    - `status` — a read returns a default "ready" value so polling loops exit;
    - `plain` — no special behaviour.
- **Data sent is captured at two levels**, so no protocol knowledge is needed:
  1. *register level*: every write to a `tx` register or transmit RAM is a
     captured byte or word on that channel. This works for raw-register code,
     typical of the 68332.
  2. *function level*: any function can be marked "transmit" with the
     argument that holds the buffer and length (library-style code).

  Captured bytes are then grouped into frames by configurable rules (gap in
  time, start/end byte, length field, fixed size) and decoded by where each
  byte came from in the code. An optional `.dbc` or user-defined layout can
  add named fields.
- **Fault codes**: found by name heuristics plus usage analysis (constants
  stored into a variable, passed to a function, or OR-ed into a flag word),
  and always user-confirmable or markable. No single fault convention is
  assumed.

**First profile: MC68332 (MC68332ACFC20).** CPU32 core, big-endian,
modules at `0xFFF000`:
- SIM: ports E/F, PIT, watchdog, chip selects;
- QSM: SCI UART with `SCDR`/`SCSR`, and QSPI with transmit and receive RAM;
- TPU: 16 timer channels plus parameter RAM;
- TPURAM.

Code for this part is usually plain C with register access through macros or
struct overlays. It may also contain K&R-style definitions, compiler keywords
like `interrupt`, and some assembly. Assembly files are not interpreted; the
functions they define become stubs. Off-chip peripherals (e.g. an external
CAN controller on a chip select) can be added to a profile as extra register
blocks.

## Architecture

```
 Source files ─► Preprocessor ─► tree-sitter ─► Lowering ─► Program model
 (.c .cpp .h .ino) (pcpp + fake     (CST)        (simplified   (functions, classes,
                    headers)                       AST)          globals, types)
                                                                    │
  Inputs (GUI) ─────────────────────────────────────────►  ┌────────▼────────┐
   • function args       • global / member variables       │   Interpreter   │◄─ Hardware model
   • stub return values  • pin / sensor values over time   │  values, scopes │   • pins (digital/analog)
                                                           │  objects, calls │   • virtual clock
                                                           │  step / break   │   • Serial / peripherals
                                                           └────────┬────────┘   • unknown-fn auto stubs
                                                                    ▼ events
                                         Trace recorder (line hit, var change, call, stub, warning)
                                                                    ▼
                                        GUI: source view, watch table, plots, narration, console
```

### Core components
1. **Frontend**
   - `preprocess.py`: runs pcpp with the bundled fake headers (`Arduino.h`,
     `stdint.h`, a minimal `stm32xx_hal.h`, ...). Keeps a mapping from
     preprocessed lines back to the original file and line.
   - `parser.py`: tree-sitter parse. Collects syntax errors as warnings, never
     fatal ones.
   - `lower.py`: turns the tree-sitter CST into a small, typed AST. Constructs
     it can't handle become `Unsupported(node)`, which logs a warning and is
     skipped when it runs.
   - `symbols.py`: functions, overloads, classes (fields, methods,
     constructors), globals, enums, namespaces.
2. **Interpreter core**
   - `values.py`: fixed-width integers (`uint8_t` wraps at 256, and so on),
     float/double, bool, char, enums, arrays, structs/class instances,
     references, pointers (object + offset), `String` (Arduino).
   - `interpreter.py`: statements and expressions, the call stack, scopes,
     `this`, constructors, simple virtual dispatch, overload resolution by
     argument count and then a best-effort type match.
   - `events.py`: a listener/event bus. The core has no Qt dependency and is
     fully testable headless.
   - **Safety guards**: limits on steps, loop iterations and wall time, so
     `while(1)` and busy-wait loops can't hang the GUI. The interpreter runs on
     a worker thread.
3. **Hardware model**
   - `stubs.py`: any function without a body becomes a stub. Its behaviour
     comes from the board profile or the user: a constant, a sequence/table,
     a function of virtual time, or "return 0 and log".
   - `clock.py`: virtual time. `delay()` advances it; `millis()` / `micros()`
     read it.
   - `pins.py`: digital and analog pin state. Inputs can be scripted over time
     (step, square wave, ramp, sine, CSV).
   - `boards/`: profiles (Arduino Uno/Nano/ESP32 first, then generic STM32
     HAL) that bundle fake headers and default stubs, e.g. `Serial`, `Wire`,
     `SPI`, `digitalRead`, `analogRead`, `HAL_GPIO_ReadPin`.
4. **Run modes**
   - *Call function*: pick any function or method, enter arguments, run it,
     and show the return value and side effects.
   - *Sketch*: `setup()` once, then `loop()` N times or for T virtual seconds.
   - *Main*: `main()` with a loop-iteration or time guard.
   - *Sweep*: repeat a run while varying one input across a range, recording
     the chosen outputs. Results go to a table and a plot ("predict outputs").
5. **Fault and message analysis** (`analysis/`, the core of the product)
   - **Fault model.** Fault codes are found automatically by heuristics, then
     the user confirms them. Candidates:
     - enums or `#define`s whose names contain `FAULT`, `ERR`, `DTC`, `ALARM`;
     - fault flag variables or bitfields, e.g. `faultFlags |= OVERTEMP_BIT`;
     - fault-reporting functions such as `setFault(code)` / `reportDTC(id)`.

     The user can also mark any variable, bit or function as a fault source.
     During a run, every set and clear is recorded with virtual time, source
     line and the call stack that caused it.
   - **Message capture.** Every transmit function becomes a capture point:
     - Arduino: `Serial.write/print`, `Wire.write`, `SPI.transfer`;
     - CAN libraries: `CAN.sendMsgBuf`, `mcp2515.sendMessage`;
     - HAL: `HAL_UART_Transmit`, `HAL_CAN_AddTxMessage`, `HAL_FDCAN_AddMessageToTxFifoQ`;
     - functions the user marks.

     Each capture records time, bus, ID/address and payload bytes, shown in
     both hex and decoded form.
   - **Decoding.** For CAN, import a `.dbc` file with `cantools` so the
     payload shows as named signals with units. Without a DBC, decode using
     the struct or array layout the code packed the bytes from: the
     interpreter knows which variable went into which byte, e.g.
     "byte 2 = batteryTemp (31)".
   - **Scenario matrix.** A table where each row is a scenario (inputs plus a
     time script) and the columns are expected or observed faults and
     messages. "Run all" fills it in. Export to CSV or Excel for reports.
     Golden results can be saved, so a later firmware version can be checked
     against them (regression).
   - **Fault index (static).** For every place in the code that sets a fault,
     list the chain of `if` / `switch` / loop conditions that must hold to
     reach it, rewritten in plain English with the variables named, e.g.
     "OVERTEMP is set when temp > 85 for longer than 500 ms
     (overTempTimer > 50) and the sensor is valid". This answers "what
     causes fault X?" without running anything.
   - **Search for a cause (later).** Given a target fault, sweep or randomly
     search the user-marked inputs within their ranges and report the input
     combinations that trigger it. Symbolic execution is a possible later
     upgrade.
6. **Trace and explanation**
   - Every event is recorded with a step number, virtual time, source location
     and old → new value.
   - **Narration**: plain-English line descriptions generated from the AST,
     e.g. `if (temp > limit)` → "Checks whether temp (31) is greater than
     limit (30): yes → enters the block".
   - "Why is this value X?": click a variable to see its history of
     assignments, each one linked to its line.
   - Coverage highlighting: lines that ran vs. lines that never ran.

## GUI (dark mode)

```
┌──────────────┬────────────────────────────────────┬──────────────────────┐
│ Project      │  Source view                       │ Inputs               │
│ ▸ Files      │  • current line highlighted        │  Run mode ▾          │
│ ▸ Functions  │  • executed lines tinted           │  Function args       │
│ ▸ Classes    │  • click gutter → breakpoint       │  Variables           │
│ ▸ Globals    │  • hover var → current value       │  Pins / sensors      │
│              │                                    │  Stubbed functions   │
│              │                                    │ [Run][Step][▶▶][Stop]│
├──────────────┴──────────────────┬─────────────────┴──────────────────────┤
│ Watch / Outputs                 │ Plot (watched vars vs time/iteration   │
│ name │ value │ changed at line  │  or vs swept input)                    │
├─────────────────────────────────┴────────────────────────────────────────┤
│ Faults timeline │ Sent messages │ Narration │ Warnings                    │
└──────────────────────────────────────────────────────────────────────────┘
```
- **Faults timeline**: one lane per fault code showing when it was set and
  cleared. Click an edge to jump to the line and see the conditions that were
  true at that moment.
- **Sent messages**: a bus-monitor style table (time, bus, ID, length, hex,
  decoded signals). Filter by ID. Click a byte to see which variable produced it.
- **Fault index** and **Scenario matrix** open as their own tabs in the
  centre area.
- A simple mode is on by default: hides types, hex and addresses. An advanced
  toggle shows them.
- **Scenarios**: save and load inputs, watches and run mode as JSON so a
  colleague can replay the exact analysis.

## C++ support scope (by phase)

| Phase | Features |
|---|---|
| Core | functions, overloads, references, `const`, `namespace`, `enum`/`enum class`, structs/classes with fields, methods, constructors, `this`, `static` members, `new`/`delete` (simple), `bool`, `nullptr`, `auto` (from initializer) |
| Arduino | global object instances (`Servo s;`), `Serial.print/println`, `String`, `#define` constants, `PROGMEM`/`F()` macros as no-ops |
| Extended | inheritance + virtual methods, operator overloading, simple function templates (instantiate on call), lambdas without captures, range-for over arrays |
| Out of scope (warn) | template metaprogramming, STL containers beyond a tiny `std::array`/`std::vector` shim, exceptions, RTTI, multiple inheritance |

## Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 ✅ | Setup | `pyproject.toml`, package layout, pytest, CI-ready, dark empty Qt window opens |
| M1 ✅ | Load and browse | Open file or folder → parse (compiler keywords blanked by the profile) → Project tree lists functions, ISRs, classes/structs, globals, constants, hardware registers (matched to the profile) and likely fault codes; source view with syntax highlighting; details and warnings panels. Full preprocessing arrives in M2, when the code is run. *Also done early: name/usage-based fault-code detection with raise/clear/check sites* |
| M2 ✅ | Interpreter core (C subset) | Expressions, control flow, functions, arrays, structs, pointers, fixed-width ints; per-feature tests |
| M3 ✅ | **MVP**: call a function | Pick a function, enter args, set globals, run, see return value and watched outputs in the GUI |
| M4 🟡 | C++ core + MC68332 hardware model | Classes and methods, pins, `delay`/`millis`, sketch and main run modes, auto-stubs. *Done: classes, constructors, inheritance and virtual methods, references, `new`; auto-stubs; register bus with tx/rx/status roles; struct layout with 68k alignment and bit-fields. Still to do: virtual clock, interrupts and timers, run modes over time* |
| M5 🟡 | **Faults and messages** | Fault auto-detection and confirmation, fault timeline, transmit capture (Serial/CAN/UART), hex plus variable-origin decoding. *Done: runtime fault raise/clear events, active faults at end, register-level transmit capture with hex dump. Still to do: confirming or editing fault candidates, a timeline view, splitting data into frames, variable-origin decoding* |
| M6 | Scenarios | Time-scripted inputs, scenario save and load, scenario matrix with Run all, CSV/Excel export, golden-result regression |
| M7 | Fault index and DBC | Static "conditions to reach this fault" in plain English; `.dbc` import for CAN decoding |
| M8 | Follow the code | Step / run-to / breakpoints, narration, coverage tint, value history, plots, sweep mode |
| M9 | More boards and C++ | Extra board profiles, inheritance/virtuals, templates, interrupts (ISRs triggered by pins or timers), search for a cause |
| M10 | Packaging | PyInstaller builds for Windows/macOS/Linux, example projects, user guide |

## Project layout

```
source_tracer/
  __main__.py
  frontend/    preprocess.py  parser.py  lower.py  symbols.py  ast.py
  core/        values.py  interpreter.py  scope.py  events.py  trace.py  narrate.py
  hw/          stubs.py  clock.py  pins.py  bus.py  boards/{arduino.py, stm32_hal.py}
  analysis/    faults.py  messages.py  decode.py (dbc, origin)  fault_index.py  matrix.py  search.py
  headers/     fake Arduino.h, stdint.h, HAL headers ...
  gui/         main_window.py  theme.py  worker.py  panels/{project, source, inputs, watch, plot, console}.py
  scenario.py
examples/      blink/, thermostat/, debounce/ (sample firmware used in demos and tests)
tests/         unit tests per language feature + end-to-end runs of examples/
```

## Testing strategy
- **Feature tests**: small C/C++ snippets, each with an expected return value
  or variable state.
- **Differential tests**: compile pure (hardware-free) functions with
  `g++` on the host and compare outputs against the interpreter, across many
  random inputs.
- **Example projects**: end-to-end runs of `examples/` with scenario files and
  expected traces.
- **GUI smoke tests**: `pytest-qt` to launch the window, load an example and
  run it.

## Risks and mitigations
| Risk | Mitigation |
|---|---|
| Vendor headers are huge and use compiler extensions | Ship small fake headers per board profile; unknown includes become empty files plus a warning |
| C++ is large | Phased scope (table above); `Unsupported` nodes warn and continue |
| Macros hide logic from non-engineers | Show both original and expanded code on hover |
| `while(1)` / busy-wait polling hangs | Iteration and time guards; hardware reads default to "ready" values |
| GUI freezes during long runs | Interpreter on a worker thread with batched event delivery |
| Faults that depend on time (debounce, "persists for 500 ms") are missed | Virtual clock plus time-scripted inputs; scenarios specify a duration, not just values |
| Fault codes hidden behind lookup tables or function pointers | User can mark fault sources by hand; capture works at the set/report function, not only on variables |
| Message bytes assembled by `memcpy` / unions / bit-shifts | Value-origin tracking through assignments, shifts and memcpy; fallback is raw hex plus DBC |
| Slow interpretation | Accept it (accuracy and speed aren't goals); optimise hot paths only if real projects need it |
