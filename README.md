# Source Tracer

A desktop tool (Qt, dark mode) for reading microcontroller firmware without
being a software engineer. Load C / C++ source and Source Tracer shows:

- its functions, interrupt handlers, variables, types and constants;
- the **hardware registers** it uses, matched to the chip's register map, and
  which ones **send data** (e.g. the MC68332 SCI data register);
- **likely fault codes**, where each one is raised, cleared or checked, and
  the variables and functions that hold fault state.

![Source Tracer showing fault codes in the MC68332 example](docs/screenshot.png)

It also **runs the code** on your PC. Pick any function, set its inputs
(arguments, starting values of variables, values returned by functions
whose code is missing, hardware register readings) and run it. You see:

- which fault codes were **raised and cleared**, where, and which faults are
  active at the end;
- every value **sent** through transmit registers (e.g. the MC68332 SCI),
  as a hex dump and byte by byte with the line that sent it;
- which variables changed, a full event log, and the lines that ran.

![Running a function: faults raised and lines executed](docs/run.png)

Coming next: scenarios over time (interrupts, timers, inputs that change
over time), a matrix of scenarios, and "what causes this fault?". See
[PLAN.md](PLAN.md).

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

source-tracer examples/mc68332_pump_controller   # or: python -m source_tracer <folder or files>
```

Then use **Open Folder…** to load your own firmware. Pick the chip under
**Target** (`MC68332`, or `Generic` for anything else).

## Using it

| Panel | What it shows |
|---|---|
| **Project** (left) | Fault codes, fault state and functions, functions, ISRs, external (assembly/library) functions, globals, hardware registers by module, types, constants, files. Type in the filter box to search. |
| **Source** (centre) | The code, with registers in red and fault codes in yellow. Clicking anything jumps here. |
| **Details** (right) | A plain-English description of the selected item: what it is, its value, where it is used. Links jump to the code. |
| **Analysis** (bottom) | *Fault codes* table, *Where faults are set / cleared* (every line that raises, clears or checks a fault), and *Warnings* (code the tool couldn't fully understand, missing functions, unknown registers). After a run, it also shows the *Run summary*, *Faults in run*, *Sent data*, *Variables (run)* and *Event log* tabs. |
| **Run** (right, next to Details) | Choose a function, then set its inputs on the tabs below: **Arguments**, **Variables** (starting values; fault variables are highlighted), **Missing functions** (what functions with no code return, e.g. `100, 200` for successive calls) and **Hardware** (what status/receive registers read). Press **Run**. Values can be numbers (`42`, `0x1F`, `-3`, `1.5`), lists (`1, 2, 3`), text (`"abc"`), struct fields (`id=1, value=2`) or names from the code (`FAULT_OVERTEMP`, `BIT_A \| BIT_B`). |

### How runs work

- The code is preprocessed (macros expanded, headers included), then
  interpreted statement by statement. Integer sizes, signedness, wrap-around,
  byte order and struct padding follow the target profile.
- Reading or writing a hardware register goes through the profile's register
  map. Writes to transmit registers are captured as sent data. Status
  registers read as "ready", so wait loops finish.
- Functions with no code (assembly, libraries) return the values you set (0
  by default), and each call is logged.
- A step limit stops endless loops such as a `main()` loop. The summary says
  where the code was when it stopped.
- Anything the interpreter can't handle becomes a warning in the summary;
  the run continues where possible.

## Adding a microcontroller

Chip knowledge lives in YAML profiles in `source_tracer/profiles/`. Copy
`mc68332.yaml` and edit:
- endianness and type sizes;
- compiler-specific keywords to ignore;
- the register map, where each register has a role:
  - `tx` — writes are transmitted data;
  - `rx` — reads come from scenario input;
  - `status` — reads return a default "ready" value;
  - `plain` — no special behaviour.

No code changes are needed.

## Development

```bash
pytest            # unit + GUI smoke tests (GUI runs headless with QT_QPA_PLATFORM=offscreen)
```

Layout: `source_tracer/frontend` (parsing, preprocessing, symbols), `core`
(types, memory model, hardware bus, interpreter), `analysis` (fault
detection, run summaries), `gui` (Qt), `profiles` (chip definitions), `examples` (sample
firmware used by tests).
