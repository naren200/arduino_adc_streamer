"""Protocol-3 acquisition phases and clock/allowance estimates, not measurements."""
from .testboard_adc124_benchmark import payload_order


def acquisition_phases(routes, mode):
    groups = []
    for route in payload_order(routes, mode):
        if not groups or (groups[-1][0].array, groups[-1][0].input) != (route.array, route.input):
            groups.append([])
        groups[-1].append(route)
    phases = []
    for group in groups:
        if mode == "mux" and phases and phases[-1][0][0].input == group[0].input:
            phases[-1].append(group)
        else:
            phases.append([group])
    return phases


def operation_counts(routes, mode, repeat=1, vmid=False, spi_frame="burst", coordinated=False, lookahead=False):
    phases = acquisition_phases(routes, mode)
    batches = sum(len(p) for p in phases)
    # Historical captures use one prime per batch. Lookahead primes only
    # during run preparation, outside the steady-state sweep budget.
    words = len(routes) * repeat + (0 if lookahead else batches) + ((4 if lookahead else 5) * batches if vmid else 0)
    cs_frames = batches * (2 if vmid else 1) if spi_frame == "burst" else words
    addresses = {r.array: 7 for r in routes}
    # Replay once to establish end-of-sweep state, then count steady-state work.
    for _ in range(2):
        switches = waits = address_writes = grouped_writes = sensor_windows = vmid_windows = 0
        for phase in phases:
            for target in ([phase[0][0].input, 7] if vmid else [phase[0][0].input]):
                changed = False
                register_masks = {}
                for group in phase:
                    array = group[0].array
                    old = addresses[array]
                    if old != target:
                        switches += 1
                        address_writes += (old ^ target).bit_count()
                        # Teensy 4.1 address-pin port mapping, independently
                        # verified against core_pins.h. Group SET/CLEAR apart.
                        ports = (6, 9, 9) if array == 1 else (9, 8, 8)
                        for bit, port in enumerate(ports):
                            if (old ^ target) & (1 << bit):
                                key = (port, bool(target & (1 << bit)))
                                if coordinated: register_masks[key] = True
                                else: grouped_writes += 1
                        addresses[array] = target
                        changed = True
                waits += changed
                if target == 7: vmid_windows += changed
                else: sensor_windows += changed
                if coordinated: grouped_writes += len(register_masks)
    return dict(sensor_batches=batches, phases=len(phases), spi_words=words, cs_frames=cs_frames,
                bank_switches=switches, settling_waits=waits, enable_writes=2 * switches,
                sensor_settling_windows=sensor_windows, vmid_settling_windows=vmid_windows,
                changed_address_bit_writes=address_writes, cs_allowance_us=.2 * cs_frames,
                grouped_address_register_writes=grouped_writes,
                mux_switch_guard_us=.4 * (waits if coordinated else switches),
                removed_cs_allowance_us=.2 * (words - cs_frames),
                basis="steady-state firmware operation counts; configured guard and clock arithmetic, not measured edge timings")
