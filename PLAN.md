# Source Tracer — Project Plan

A desktop tool (Qt, dark mode) that loads microcontroller source code (C and C++,
including Arduino-style sketches), interprets it on the PC, lets the user set
inputs (function arguments, variables, pins, stubbed hardware calls) and shows
chosen variables as outputs.

## Goals and non-goals

**Primary users:** non-software engineers who need to *follow* and *predict*
the behaviour of existing, working firmware.

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
5. **Trace and explanation**
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
│ Narration │ Serial output │ Warnings (unsupported/skipped code)          │
└──────────────────────────────────────────────────────────────────────────┘
```
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
| M0 | Setup | `pyproject.toml`, package layout, pytest, CI-ready, dark empty Qt window opens |
| M1 | Load and browse | Open file or folder → preprocess → parse → Project tree lists functions, classes, globals; source view with syntax highlighting; warnings panel |
| M2 | Interpreter core (C subset) | Expressions, control flow, functions, arrays, structs, pointers, fixed-width ints; per-feature tests |
| M3 | **MVP**: call a function | Pick a function, enter args, set globals, run, see return value and watched outputs in the GUI |
| M4 | C++ core + Arduino profile | Classes and methods, `Serial`, pins, `delay`/`millis`, sketch run mode, auto-stubs |
| M5 | Follow the code | Step / run-to / breakpoints, current-line highlight, narration, coverage tint, value history |
| M6 | Predict outputs | Time-varying inputs, plots, sweep mode, scenario save and load |
| M7 | More boards and C++ | STM32 HAL profile, inheritance/virtuals, templates, interrupts (ISRs triggered by pin changes or timers) |
| M8 | Packaging | PyInstaller builds for Windows/macOS/Linux, example projects, user guide |

## Project layout

```
source_tracer/
  __main__.py
  frontend/    preprocess.py  parser.py  lower.py  symbols.py  ast.py
  core/        values.py  interpreter.py  scope.py  events.py  trace.py  narrate.py
  hw/          stubs.py  clock.py  pins.py  boards/{arduino.py, stm32_hal.py}
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
| Slow interpretation | Accept it (accuracy and speed aren't goals); optimise hot paths only if real projects need it |
