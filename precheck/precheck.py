#!/usr/bin/env python3
import argparse
import logging
import os
import re
import subprocess
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

import brotli
import gdstk
import klayout.db as pya
import klayout.rdb as rdb
import yaml
from klayout_tools import parse_lyp_layers
from pin_check import pin_check
from precheck_failure import PrecheckFailure
from tech_data import analog_pin_pos, layer_map, valid_layers

PDK_ROOT = os.getenv("PDK_ROOT")
PDK_NAME = os.getenv("PDK_NAME") or "sky130A"
LYP_FILE = f"{PDK_ROOT}/{PDK_NAME}/libs.tech/klayout/tech/{PDK_NAME}.lyp"
REPORTS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "reports")

if not PDK_ROOT:
    logging.error("PDK_ROOT environment variable not set")
    exit(1)


def has_sky130_devices(gds: str):
    for cell_name in gdstk.read_rawcells(gds):
        if cell_name.startswith("sky130_fd_"):
            return True
    return False


def magic_drc(gds: str, toplevel: str):
    logging.info(f"Running magic DRC on {gds} (module={toplevel})")

    magic = subprocess.run(
        [
            "magic",
            "-noconsole",
            "-dnull",
            "-rcfile",
            f"{PDK_ROOT}/{PDK_NAME}/libs.tech/magic/{PDK_NAME}.magicrc",
            "magic_drc.tcl",
            gds,
            toplevel,
            PDK_ROOT,
            f"{REPORTS_PATH}/magic_drc.txt",
            f"{REPORTS_PATH}/magic_drc.mag",
        ],
    )

    if magic.returncode != 0:
        if not has_sky130_devices(gds):
            logging.warning("No sky130 devices present - was the design flattened?")
        raise PrecheckFailure("Magic DRC failed")


def klayout_drc(gds: str, check: str, script=f"{PDK_NAME}_mr.drc", extra_vars=[]):
    logging.info(f"Running klayout {check} on {gds}")
    report_file = f"{REPORTS_PATH}/drc_{check}.xml"
    script_vars = [
        f"{check}=true",
        f"input={gds}",
        f"report={report_file}",
    ]
    klayout_args = ["klayout", "-b", "-r", f"tech-files/{script}"]
    for v in script_vars + extra_vars:
        klayout_args.extend(["-rd", v])
    klayout = subprocess.run(klayout_args)
    if klayout.returncode != 0:
        raise PrecheckFailure(f"Klayout {check} failed")

    report = rdb.ReportDatabase("DRC")
    report.load(report_file)

    if report.num_items() > 0:
        raise PrecheckFailure(
            f"Klayout {check} failed with {report.num_items()} DRC violations"
        )


def klayout_zero_area(gds: str):
    return klayout_drc(gds, "zero_area", "zeroarea.rb.drc")


def klayout_checks(gds: str, expected_name: str):
    layout = pya.Layout()
    layout.read(gds)
    layers = parse_lyp_layers(LYP_FILE)

    logging.info("Running top macro name check...")
    top_cell = layout.top_cell()
    if top_cell.name != expected_name:
        raise PrecheckFailure(
            f"Top macro name mismatch: expected {expected_name}, got {top_cell.name}"
        )

    logging.info("Running forbidden layer check...")
    forbidden_layers = [
        "met5.drawing",
        "met5.pin",
        "met5.label",
    ]

    for layer in forbidden_layers:
        layer_info = layers[layer]
        logging.info(f"* Checking {layer_info.name}")
        layer_index = layout.find_layer(layer_info.layer, layer_info.data_type)
        if layer_index is not None:
            raise PrecheckFailure(f"Forbidden layer {layer} found in {gds}")

    logging.info("Running prBoundary check...")
    layer_info = layers["prBoundary.boundary"]
    layer_index = layout.find_layer(layer_info.layer, layer_info.data_type)
    if layer_index is None:
        calma_index = f"{layer_info.layer}/{layer_info.data_type}"
        raise PrecheckFailure(
            f"prBoundary.boundary ({calma_index}) layer not found in {gds}"
        )


def boundary_check(gds: str):
    """Ensure that there are no shapes outside the project area."""
    lib = gdstk.read_gds(gds)
    tops = lib.top_level()
    if len(tops) != 1:
        raise PrecheckFailure("GDS top level not unique")
    top = tops[0]
    boundary = top.copy("test_boundary")
    boundary.filter([(235, 4)], False)
    if top.bounding_box() != boundary.bounding_box():
        raise PrecheckFailure("Shapes outside project area")


def power_pin_check(verilog: str, lef: str, uses_3v3: bool):
    """Ensure that VPWR / VGND are present,
    and that VAPWR is present if and only if 'uses_3v3' is set."""
    verilog_s = open(verilog).read().replace("VPWR", "VDPWR")
    lef_s = open(lef).read().replace("VPWR", "VDPWR")

    # naive but good enough way to ignore comments
    verilog_s = re.sub("//.*", "", verilog_s)
    verilog_s = re.sub("/\\*.*\\*/", "", verilog_s, flags=(re.DOTALL | re.MULTILINE))

    for ft, s in (("Verilog", verilog_s), ("LEF", lef_s)):
        for pwr, ex in (("VGND", True), ("VDPWR", True), ("VAPWR", uses_3v3)):
            if (pwr in s) and not ex:
                raise PrecheckFailure(f"{ft} contains {pwr}")
            if not (pwr in s) and ex:
                raise PrecheckFailure(f"{ft} doesn't contain {pwr}")


def layer_check(gds: str, tech: str):
    """Check that there are no invalid layers in the GDS file."""
    lib = gdstk.read_gds(gds)
    layers = lib.layers_and_datatypes().union(lib.layers_and_texttypes())
    excess = layers - valid_layers[tech]
    if excess:
        raise PrecheckFailure(f"Invalid layers in GDS: {excess}")


def cell_name_check(gds: str):
    """Check that there are no cell names with '#' or '/' in them."""
    for cell_name in gdstk.read_rawcells(gds):
        if "#" in cell_name:
            raise PrecheckFailure(
                f"Cell name {cell_name} contains invalid character '#'"
            )
        if "/" in cell_name:
            raise PrecheckFailure(
                f"Cell_name {cell_name} contains invalid character '/'"
            )


def urpm_nwell_check(gds: str, top_module: str):
    """Run a DRC check for urpm to nwell spacing."""
    extra_vars = [f"thr={os.cpu_count()}", f"top_cell={top_module}"]
    klayout_drc(
        gds=gds, check="nwell_urpm", script="nwell_urpm.drc", extra_vars=extra_vars
    )


def analog_pin_check(
    gds: str, tech: str, is_analog: bool, uses_3v3: bool, analog_pins: int, pinout: dict
):
    """Check that every analog pin connects to a piece of metal
    if and only if the pin is used according to info.yaml."""
    if is_analog:
        lib = gdstk.read_gds(gds)
        top = lib.top_level()[0]
        met4 = top.copy("test_met4")
        met4.flatten()
        met4.filter([layer_map[tech]["met4"]], False)
        via3 = top.copy("test_via3")
        via3.flatten()
        via3.filter([layer_map[tech]["via3"]], False)

        for pin in range(8):
            x = analog_pin_pos[tech](pin, uses_3v3)
            x1, y1, x2, y2 = x, 0, x + 0.9, 1.0
            pin_over = gdstk.rectangle((x1, y1), (x2, y2))
            pin_above = gdstk.rectangle((x1, y2 + 0.1), (x2, y2 + 0.5))
            pin_below = gdstk.rectangle((x1, y1 - 0.5), (x2, y1 - 0.1))
            pin_left = gdstk.rectangle((x1 - 0.5, y1), (x1 - 0.1, y2))
            pin_right = gdstk.rectangle((x2 + 0.1, y1), (x2 + 0.5, y2))

            via3_over = gdstk.boolean(via3.polygons, pin_over, "and")
            met4_above = gdstk.boolean(met4.polygons, pin_above, "and")
            met4_below = gdstk.boolean(met4.polygons, pin_below, "and")
            met4_left = gdstk.boolean(met4.polygons, pin_left, "and")
            met4_right = gdstk.boolean(met4.polygons, pin_right, "and")

            connected = (
                bool(via3_over)
                or bool(met4_above)
                or bool(met4_below)
                or bool(met4_left)
                or bool(met4_right)
            )
            expected_pc = pin < analog_pins
            expected_pd = bool(pinout.get(f"ua[{pin}]", ""))

            if connected and not expected_pc:
                raise PrecheckFailure(
                    f"Analog pin {pin} connected but `analog_pins` is {analog_pins}"
                )
            elif connected and not expected_pd:
                raise PrecheckFailure(
                    f"Analog pin {pin} connected but `pinout.ua[{pin}]` is falsy"
                )
            elif not connected and expected_pc:
                raise PrecheckFailure(
                    f"Analog pin {pin} not connected but `analog_pins` is {analog_pins}"
                )
            elif not connected and expected_pd:
                raise PrecheckFailure(
                    f"Analog pin {pin} not connected but `pinout.ua[{pin}]` is truthy"
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gds", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.info(f"PDK_ROOT: {PDK_ROOT}")

    if args.gds.endswith(".gds"):
        gds_stem = args.gds.removesuffix(".gds")
        gds_temp = None
        gds_file = args.gds
    elif args.gds.endswith(".gds.br"):
        gds_stem = args.gds.removesuffix(".gds.br")
        gds_temp = tempfile.NamedTemporaryFile(suffix=".gds", delete=False)
        gds_file = gds_temp.name
        logging.info(f"decompressing {args.gds} to {gds_file}")
        with open(args.gds, "rb") as f:
            data = f.read()
        with open(gds_file, "wb") as f:
            f.write(brotli.decompress(data))
    else:
        raise PrecheckFailure("Layout file extension is neither .gds nor .gds.br")

    yaml_dir = os.path.dirname(args.gds)
    while not os.path.exists(f"{yaml_dir}/info.yaml"):
        yaml_dir = os.path.dirname(yaml_dir)
        if yaml_dir == "/":
            raise PrecheckFailure("info.yaml not found")
    yaml_file = f"{yaml_dir}/info.yaml"
    yaml_data = yaml.safe_load(open(yaml_file))
    logging.info("info.yaml data:" + str(yaml_data))

    wokwi_id = yaml_data["project"].get("wokwi_id", 0)
    top_module = yaml_data["project"].get("top_module", f"tt_um_wokwi_{wokwi_id}")
    assert top_module == os.path.basename(gds_stem)

    tiles = yaml_data.get("project", {}).get("tiles", "1x1")
    analog_pins = yaml_data.get("project", {}).get("analog_pins", 0)
    is_analog = analog_pins > 0
    uses_3v3 = bool(yaml_data.get("project", {}).get("uses_3v3", False))
    pinout = yaml_data.get("pinout", {})
    if uses_3v3 and not is_analog:
        raise PrecheckFailure("Projects with 3v3 power need at least one analog pin")
    if is_analog:
        if uses_3v3:
            template_def = f"../def/analog/tt_analog_{tiles}_3v3.def"
        else:
            template_def = f"../def/analog/tt_analog_{tiles}.def"
    else:
        template_def = f"../def/tt_block_{tiles}_pg.def"
    logging.info(f"using def template {template_def}")

    lef_file = gds_stem + ".lef"
    verilog_file = gds_stem + ".v"
    tech = "sky130"

    checks = [
        ["Magic DRC", lambda: magic_drc(gds_file, top_module)],
        ["KLayout FEOL", lambda: klayout_drc(gds_file, "feol")],
        ["KLayout BEOL", lambda: klayout_drc(gds_file, "beol")],
        ["KLayout offgrid", lambda: klayout_drc(gds_file, "offgrid")],
        [
            "KLayout pin label overlapping drawing",
            lambda: klayout_drc(
                gds_file,
                "pin_label_purposes_overlapping_drawing",
                "pin_label_purposes_overlapping_drawing.rb.drc",
            ),
        ],
        ["KLayout zero area", lambda: klayout_zero_area(gds_file)],
        ["KLayout Checks", lambda: klayout_checks(gds_file, top_module)],
        [
            "Pin check",
            lambda: pin_check(gds_file, lef_file, template_def, top_module, uses_3v3),
        ],
        ["Boundary check", lambda: boundary_check(gds_file)],
        ["Power pin check", lambda: power_pin_check(verilog_file, lef_file, uses_3v3)],
        ["Layer check", lambda: layer_check(gds_file, tech)],
        ["Cell name check", lambda: cell_name_check(gds_file)],
        ["urpm/nwell check", lambda: urpm_nwell_check(gds_file, top_module)],
        [
            "Analog pin check",
            lambda: analog_pin_check(
                gds_file, tech, is_analog, uses_3v3, analog_pins, pinout
            ),
        ],
    ]

    testsuite = ET.Element("testsuite", name="Tiny Tapeout Prechecks")
    error_count = 0
    markdown_table = "# Tiny Tapeout Precheck Results\n\n"
    markdown_table += "| Check | Result |\n|-----------|--------|\n"
    for [name, check] in checks:
        start_time = time.time()
        test_case = ET.SubElement(testsuite, "testcase", name=name)
        try:
            check()
            elapsed_time = time.time() - start_time
            markdown_table += f"| {name} | ✅ |\n"
            test_case.set("time", str(round(elapsed_time, 2)))
        except Exception as e:
            error_count += 1
            elapsed_time = time.time() - start_time
            markdown_table += f"| {name} | ❌ Fail: {str(e)} |\n"
            test_case.set("time", str(round(elapsed_time, 2)))
            error = ET.SubElement(test_case, "error", message=str(e))
            error.text = traceback.format_exc()
    markdown_table += "\n"
    markdown_table += "In case of failure, please reach out on [discord](https://tinytapeout.com/discord) for assistance."

    testsuites = ET.Element("testsuites")
    testsuites.append(testsuite)
    xunit_report = ET.ElementTree(testsuites)
    ET.indent(xunit_report, space="  ", level=0)
    xunit_report.write(f"{REPORTS_PATH}/results.xml", encoding="unicode")

    with open(f"{REPORTS_PATH}/results.md", "w") as f:
        f.write(markdown_table)

    if gds_temp is not None:
        gds_temp.close()
        os.unlink(gds_temp.name)

    if error_count > 0:
        logging.error(f"Precheck failed for {args.gds}! 😭")
        logging.error(f"See {REPORTS_PATH} for more details")
        logging.error(f"Markdown report:\n{markdown_table}")
        exit(1)
    else:
        logging.info(f"Precheck passed for {args.gds}! 🎉")


if __name__ == "__main__":
    main()
