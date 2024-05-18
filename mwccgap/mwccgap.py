import os
import re
import subprocess
import sys
import tempfile

from pathlib import Path
from typing import List, Optional

from .elf import Elf, TextSection


INCLUDE_ASM = "INCLUDE_ASM"
INCLUDE_ASM_REGEX = r'INCLUDE_ASM\("(.*)", (.*)\)'


def assemble_file(asm_filepath: Path, as_path="mipsel-linux-gnu-as", as_flags: Optional[List[str]]=None) -> bytes:
    if as_flags is None:
        as_flags = []

    with tempfile.NamedTemporaryFile(suffix=".o") as temp_file:
        cmd = [
            as_path,
            "-EL",
            "-march=gs464",
            "-mabi=32",
            "-Iinclude",
            "-o",
            temp_file.name,
            *as_flags,
        ]
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE
        ) as process:
            stdout, stderr = process.communicate(input=asm_filepath.read_bytes())

        if len(stdout) > 0:
            sys.stderr.write(stdout.decode("utf"))
        if len(stderr) > 0:
            sys.stderr.write(stderr.decode("utf"))

        obj_bytes = temp_file.read()
        if len(obj_bytes) == 0:
            raise Exception(f"Failed to assemble {asm_filepath} (object is empty)")

    return obj_bytes


def preprocess_c_file(c_file, asm_dir_prefix=None) -> tuple[List[str], List[Path]]:
    with open(c_file, "r") as f:
        lines = f.readlines()

    out_lines: List[str] = []
    asm_files: List[Path] = []
    for i, line in enumerate(lines):
        line = line.rstrip()

        if line.startswith(INCLUDE_ASM):
            if not (match := re.match(INCLUDE_ASM_REGEX, line)):
                raise Exception(
                    f"{c_file} contains invalid {INCLUDE_ASM} macro on line {i}: {line}"
                )
            try:
                asm_dir = Path(match.group(1))
                asm_function = match.group(2)
            except Exception as e:
                raise Exception(
                    f"{c_file} contains invalid {INCLUDE_ASM} macro on line {i}: {line}"
                ) from e

            asm_file = asm_dir / f"{asm_function}.s"
            if asm_dir_prefix is not None:
                asm_file = asm_dir_prefix / asm_file

            if not asm_file.is_file():
                raise Exception(
                    f"{c_file} includes asm {asm_file} that does not exist on line {i}: {line}"
                )
            asm_files.append(asm_file)

            nops_needed = 0
            for asm_line in asm_file.read_text().split("\n"):
                asm_line = asm_line.strip()
                if not asm_line:
                    # skip empty
                    continue
                if asm_line.startswith(".set"):
                    # ignore set
                    continue
                if asm_line.startswith(".include"):
                    # ignore include
                    continue
                if asm_line.startswith(".size"):
                    # ignore size
                    continue
                if asm_line.startswith(".align") or asm_line.startswith(".balign"):
                    # ignore alignment
                    continue
                if asm_line.startswith("glabel") or asm_line.startswith("jlabel"):
                    # ignore function / jumptable labels
                    continue
                if asm_line.startswith(".L") and asm_line.endswith(":"):
                    # ignore labels
                    continue
                if asm_line.startswith("/* Generated by spimdisasm"):
                    # ignore spim
                    continue

                if ".rodata" in asm_line:
                    raise Exception("RODATA IS NOT CURRENTLY SUPPORTED!")

                nops_needed += 1

            # TODO: align to 8 bytes for asm-differ?

            nops = nops_needed * ["nop"]
            out_lines.extend([f"asm void {asm_function}() {'{'}", *nops, "}"])

        else:
            out_lines.append(line)

    return (out_lines, asm_files)


def compile_file(
    c_file: Path,
    o_file: Path,
    c_flags=None,
    mwcc_path="mwccpsp.exe",
    use_wibo=True,
    wibo_path="wibo",
):
    if c_flags is None:
        c_flags = []

    o_file.parent.mkdir(exist_ok=True, parents=True)
    o_file.unlink(missing_ok=True)

    cmd = [
        mwcc_path,
        "-c",
        *c_flags,
        "-o",
        str(o_file),
        str(c_file),
    ]
    if use_wibo:
        cmd.insert(0, wibo_path)

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, MWCIncludes="."),
    ) as proc:
        return proc.communicate()


def process_c_file(
    c_file: Path,
    o_file: Path,
    c_flags=None,
    mwcc_path="mwccpsp.exe",
    as_path="mipsel-linux-gnu-as",
    as_flags=None,
    use_wibo=True,
    wibo_path="wibo",
    asm_dir_prefix=None,
):
    # 1. compile file as-is, any INCLUDE_ASM'd functions will be missing
    # TODO: is there a better way to do this?
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_o_file = Path(temp_dir) / "precompile.o"
        stdout, stderr = compile_file(
            c_file,
            temp_o_file,
            c_flags=c_flags,
            mwcc_path=mwcc_path,
            use_wibo=use_wibo,
            wibo_path=wibo_path,
        )

        if len(stdout) > 0:
            sys.stderr.write(stdout.decode("utf"))
        if len(stderr) > 0:
            sys.stderr.write(stderr.decode("utf"))

        if not temp_o_file.is_file():
            raise Exception(f"Error precompiling {c_file}")

        obj_bytes = temp_o_file.read_bytes()
        if len(obj_bytes) == 0:
            raise Exception(f"Error precompiling {c_file}, object is empty")

    precompiled_elf = Elf(obj_bytes)

    # for now we only care about the names of the functions that exist
    c_functions = [f.function_name for f in precompiled_elf.get_functions()]

    # 2. identify all INCLUDE_ASM statements and replace with asm statements full of nops
    out_lines, asm_files = preprocess_c_file(c_file, asm_dir_prefix=asm_dir_prefix)

    # filter out functions that can be found in the compiled c object
    asm_files = [x for x in asm_files if x.stem not in c_functions]

    # 3. compile the modified .c file for real
    with tempfile.NamedTemporaryFile(suffix=".c_", dir=c_file.parent) as temp_c_file:
        temp_c_file.write("\n".join(out_lines).encode("utf"))
        temp_c_file.flush()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_o_file = Path(temp_dir) / "result.o"

            stdout, stderr = compile_file(
                Path(temp_c_file.name),
                temp_o_file,
                c_flags,
                mwcc_path=mwcc_path,
                use_wibo=use_wibo,
                wibo_path=wibo_path,
            )

            if len(stdout) > 0:
                sys.stderr.write(stdout.decode("utf"))
            if len(stderr) > 0:
                sys.stderr.write(stderr.decode("utf"))

            if not temp_o_file.is_file():
                raise Exception(f"Error compiling {c_file}")

            obj_bytes = temp_o_file.read_bytes()
            if len(obj_bytes) == 0:
                raise Exception(f"Error compiling {c_file}, object is empty")

    if len(asm_files) == 0:
        sys.stderr.write(
            f"WARNING: No {INCLUDE_ASM} macros found in source file {c_file}\n"
        )
        o_file.parent.mkdir(exist_ok=True, parents=True)
        with o_file.open("wb") as f:
            f.write(obj_bytes)
        return

    compiled_elf = Elf(obj_bytes)

    rel_text_sh_name = compiled_elf.add_sh_symbol(".rel.text")

    for asm_file in asm_files:
        function = asm_file.stem

        asm_bytes = assemble_file(asm_file, as_path=as_path, as_flags=as_flags)
        assembled_elf = Elf(asm_bytes)
        asm_functions = assembled_elf.get_functions()
        assert len(asm_functions) == 1, "Only support 1 function per asm file"

        # identify the .text section for this function
        for index, section in enumerate(compiled_elf.sections):
            if isinstance(section, TextSection) and section.function_name == function:
                break
        else:
            raise Exception(f"{function} not found in {c_file}")

        asm_text = asm_functions[0].data
        compiled_function_length = len(section.data)

        assert (
            len(asm_text) >= compiled_function_length
        ), f"Not enough assembly to fill {function} in {c_file}"

        section.data = asm_text[:compiled_function_length]

        relocation_records = assembled_elf.get_relocations()
        assert (
            len(relocation_records) < 2
        ), f"{asm_file} has too many relocation records!"

        reloc_symbols = set()
        for relocation_record in relocation_records:
            relocation_record.sh_link = compiled_elf.symtab_index
            relocation_record.sh_name = rel_text_sh_name
            relocation_record.sh_info = index

            for relocation in relocation_record.relocations:
                symbol = assembled_elf.symtab.symbols[relocation.symbol_index]
                idx = compiled_elf.add_symbol(symbol)
                relocation.symbol_index = idx
                reloc_symbols.add(symbol.name)

            compiled_elf.add_section(relocation_record)

        for symbol in assembled_elf.symtab.symbols:
            if symbol.st_name == 0:
                continue
            if symbol.name not in reloc_symbols:
                symbol.st_shndx = index
                compiled_elf.add_symbol(symbol)

    o_file.parent.mkdir(exist_ok=True, parents=True)
    with o_file.open("wb") as f:
        f.write(compiled_elf.pack())
