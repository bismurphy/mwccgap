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

FUNCTION_PREFIX = "mwccgap_"


def assemble_file(
    asm_filepath: Path,
    as_path="mipsel-linux-gnu-as",
    as_flags: Optional[List[str]] = None,
) -> bytes:
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

            in_rodata = False
            rodata_entries = {}
            nops_needed = 0

            for asm_line in asm_file.read_text().split("\n"):
                asm_line = asm_line.strip()
                if not asm_line:
                    # skip empty lines
                    continue

                if asm_line.startswith(".section"):
                    if asm_line.endswith(".text"):
                        in_rodata = False
                        continue
                    elif asm_line.endswith(".rodata"):
                        in_rodata = True
                        continue

                    raise Exception(f"Unsupported .section: {asm_line}")

                if in_rodata:
                    if asm_line.startswith(".align"):
                        continue
                    if asm_line.startswith(".size"):
                        continue
                    if asm_line.startswith("glabel"):
                        _, rodata_symbol = asm_line.split(" ")
                        rodata_entries[rodata_symbol] = 0
                        continue
                    if asm_line.find(" .word ") > -1:
                        rodata_entries[rodata_symbol] += 4
                        continue

                    raise Exception(f"Unexpected entry in .rodata: {asm_line}")

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

                nops_needed += 1

            nops = nops_needed * ["nop"]
            out_lines.extend(
                [f"asm void {FUNCTION_PREFIX}{asm_function}() {'{'}", *nops, "}"]
            )

            for symbol, size in rodata_entries.items():
                words_needed = size // 4
                out_lines.append(
                    f"const long {symbol}[{words_needed}] = {'{'}"
                    + words_needed * "0, "
                    + "};",
                )

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

    # 2. identify all INCLUDE_ASM statements and replace with asm statements full of nops
    out_lines, asm_files = preprocess_c_file(c_file, asm_dir_prefix=asm_dir_prefix)

    # for now we only care about the names of the functions that exist
    c_functions = [f.function_name for f in precompiled_elf.get_functions()]

    # filter out functions that can be found in the compiled c object
    asm_files = [x for x in asm_files if x.stem not in c_functions]

    # if there's nothing to do, write out the bytes from the precompiled object
    if len(asm_files) == 0:
        o_file.parent.mkdir(exist_ok=True, parents=True)
        with o_file.open("wb") as f:
            f.write(obj_bytes)
        return

    # 3. compile the modified .c file for real
    with tempfile.NamedTemporaryFile(suffix=".c", dir=c_file.parent) as temp_c_file:
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

    compiled_elf = Elf(obj_bytes)

    rel_text_sh_name = compiled_elf.add_sh_symbol(".rel.text")

    for symbol in compiled_elf.symtab.symbols:
        if symbol.name.startswith(FUNCTION_PREFIX):
            symbol.name = symbol.name[len(FUNCTION_PREFIX) :]
            symbol.st_name += len(FUNCTION_PREFIX)

    for asm_file in asm_files:
        function = asm_file.stem

        asm_bytes = assemble_file(asm_file, as_path=as_path, as_flags=as_flags)
        assembled_elf = Elf(asm_bytes)
        asm_functions = assembled_elf.get_functions()
        assert len(asm_functions) == 1, "Only support 1 function per asm file"

        # identify the .text section for this function
        for text_section_index, text_section in enumerate(compiled_elf.sections):
            if (
                isinstance(text_section, TextSection)
                and text_section.function_name == f"{FUNCTION_PREFIX}{function}"
            ):
                break
        else:
            raise Exception(f"{function} not found in {c_file}")

        rodata_section_index = -1
        for i, rodata_section in enumerate(
            compiled_elf.sections[text_section_index + 1 :]
        ):
            if rodata_section.name == "":
                # found another .text section before .rodata
                rodata_section_index = -1
                break
            if rodata_section.name == ".rodata":
                # found .rodata before another .text section
                rodata_section_index = text_section_index + 1 + i
                break

        asm_text = asm_functions[0].data
        compiled_function_length = len(text_section.data)

        has_rodata = rodata_section_index > -1

        if has_rodata:
            assert (
                len(assembled_elf.rodata_sections) == 1
            ), "Expected ASM to contain 1 .rodata section"
            asm_rodata = assembled_elf.rodata_sections[0]
            print(rodata_section)

        assert (
            len(asm_text) >= compiled_function_length
        ), f"Not enough assembly to fill {function} in {c_file}"

        text_section.data = asm_text[:compiled_function_length]

        if has_rodata:
            rodata_section.data = asm_rodata.data
            rel_rodata_sh_name = compiled_elf.add_sh_symbol(".rel.rodata")

        relocation_records = assembled_elf.get_relocations()
        assert (
            len(relocation_records) < 3
        ), f"{asm_file} has too many relocation records!"

        reloc_symbols = set()

        initial_sh_info_value = compiled_elf.symtab.sh_info
        local_syms_inserted = 0

        # assumes .text relocations precede .rodata relocations
        for i, relocation_record in enumerate(relocation_records):
            relocation_record.sh_link = compiled_elf.symtab_index
            if i == 0:
                relocation_record.sh_name = rel_text_sh_name
                relocation_record.sh_info = text_section_index
            if i == 1:
                relocation_record.sh_name = rel_rodata_sh_name
                relocation_record.sh_info = rodata_section_index

            for relocation in relocation_record.relocations:
                symbol = assembled_elf.symtab.symbols[relocation.symbol_index]

                if symbol.bind == 0:
                    local_syms_inserted += 1

                idx = compiled_elf.add_symbol(symbol, force=i == 1)
                relocation.symbol_index = idx
                reloc_symbols.add(symbol.name)

                if i == 1:
                    # repoint .rodata reloc to .text section
                    symbol.st_shndx = text_section_index

            compiled_elf.add_section(relocation_record)

        if local_syms_inserted > 0:
            # update relocations
            for relocation_record in compiled_elf.get_relocations():

                if relocation_record.sh_info == rodata_section_index:
                    # don't touch the .rodata relocs...
                    continue

                for relocation in relocation_record.relocations:
                    if relocation.symbol_index >= initial_sh_info_value:
                        relocation.symbol_index += local_syms_inserted

        for symbol in assembled_elf.symtab.symbols:
            if symbol.st_name == 0:
                continue
            if symbol.name not in reloc_symbols:
                symbol.st_shndx = text_section_index
                compiled_elf.add_symbol(symbol)

    o_file.parent.mkdir(exist_ok=True, parents=True)
    with o_file.open("wb") as f:
        f.write(compiled_elf.pack())
