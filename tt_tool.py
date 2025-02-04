#!/usr/bin/env python3
import argparse
import logging
import sys

from project import Project

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TT setup")
    sub_parsers = parser.add_subparsers(dest="command", help="subcommand")
    docsparser = sub_parsers.add_parser('docs')
    fpga_parser = sub_parsers.add_parser('fpga')
    info_parser = sub_parsers.add_parser('info')
    
    parser.add_argument("--project-dir", help="location of the project", default=".")
    parser.add_argument(
        "--yaml", help="the project's yaml configuration file", default="info.yaml"
    )
    parser.add_argument(
        "--debug",
        help="debug logging",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
    )
    parser.add_argument(
        "--openlane2",
        help="use openlane 2",
        action="store_const",
        const=True,
        default=False,
    )
    parser.add_argument(
        "--orfs",
        help="use ORFS",
        action="store_const",
        const=True,
        default=False,
    )

    # reports & summaries
    parser.add_argument(
        "--print-cell-summary",
        help="print summary",
        action="store_const",
        const=True,
        default=False,
    )
    parser.add_argument(
        "--print-cell-category",
        help="print category",
        action="store_const",
        const=True,
        default=False,
    )
    parser.add_argument(
        "--print-stats",
        help="print some stats from the run",
        action="store_const",
        const=True,
    )
    parser.add_argument(
        "--print-warnings", help="print any warnings", action="store_const", const=True
    )
    parser.add_argument(
        "--print-wokwi-id",
        help="prints the Wokwi project id",
        action="store_const",
        const=True,
    )
    parser.add_argument(
        "--print-top-module",
        help="prints the name of the top module",
        action="store_const",
        const=True,
    )

	
    
    parser.add_argument(
        "--check-docs",
        help="check the documentation part of the yaml",
        action="store_const",
        const=True,
    )
    
    # documentation
    docsparser.add_argument(
        "--create-pdf",
        help="create a single page PDF",
        action="store_const",
        const=True,
    )
    docsparser.add_argument(
        "--create-svg",
        help="create a svg of the GDS layout",
        action="store_const",
        const=True,
    )
    docsparser.add_argument(
        "--create-png",
        help="create a png of the GDS layout",
        action="store_const",
        const=True,
    )

    # configure
    parser.add_argument(
        "--create-user-config",
        help="create the user_config.json file with top module and source files",
        action="store_const",
        const=True,
    )
    parser.add_argument(
        "--harden",
        help="use a local OpenLane install to harden the project",
        action="store_const",
        const=True,
    )

    # FPGA
    fpga_parser.add_argument('--create-bitstream', required=False, action='store_true', help='Generate FPGA bitstream')
    fpga_parser.add_argument(
        "--board",
        help="select the FPGA board",
        choices=('tt_fpga', 'efab_explain'),
        default='tt_fpga',
        required=False
    )
    
    info_parser.add_argument('--clock_hz', required=False, action='store_true', help='Get the specified clock rate')
    info_parser.add_argument('--title', required=False, action='store_true', help='Get the specified project title')
    

    args = parser.parse_args()

    # setup log
    log_format = logging.Formatter(
        "%(asctime)s - %(module)-10s - %(levelname)-8s - %(message)s"
    )
    # configure the client logging
    log = logging.getLogger("")
    # has to be set to debug as is the root logger
    log.setLevel(args.loglevel)

    # create console handler and set level to info
    ch = logging.StreamHandler(sys.stdout)
    # create formatter for console
    ch.setFormatter(log_format)
    log.addHandler(ch)

    project = Project(0, "unknown", args.project_dir, args, is_user_project=True)
    project.post_clone_setup()

    # handle the options
    if args.command == 'info':
        if args.clock_hz:
            print(project.info.clock_hz, end='')
        if args.title:
            print(project.info.title, end='')
    
    
    if args.check_docs:
        project.check_docs()
        
    if args.print_cell_summary or args.print_cell_category:
        project.summarize()

    if args.print_stats:
        project.print_stats()

    if args.print_warnings:
        project.print_warnings()

    if args.print_wokwi_id:
        project.print_wokwi_id()

    if args.print_top_module:
        project.print_top_module()

    if args.create_user_config:
        project.create_user_config()

    if args.harden:
        project.harden()

    if args.command == 'docs':
            
        if args.create_pdf:
            project.create_pdf()

        if args.create_png:
            project.create_png()

        if args.create_svg:
            project.create_svg()

    if args.command == 'fpga' and args.create_bitstream:
        project.create_fpga_bitstream()
