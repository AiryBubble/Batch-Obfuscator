from __future__ import annotations
import argparse
import logging
import random
import re
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Set

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ALPHABET = list("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ @=")
KEY_PREFIX = "VAR"
NUM_KEYS = 5

OPERATORS = ["&&", "||", ">>", "2>>", "2>", "1>", "1>>", ">|", "&", "|", ">", "<", ";"]
OPERATORS.sort(key=len, reverse=True)

LABEL_PATTERN = re.compile(r"^:\s*\w+")
SET_PATTERN = re.compile(r'^\s*set\s+["\']?([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE)
SET_WITH_VALUE_PATTERN = re.compile(r'^\s*set\s+["\']?([a-zA-Z_][a-zA-Z0-9_]*)=', re.IGNORECASE)
SET_ASSIGN_PATTERN = re.compile(r'^\s*set\s+["\']?([a-zA-Z_][a-zA-Z0-9_]*)\s*=', re.IGNORECASE)

SYSTEM_VARIABLES = {
    "%random%", "%errorlevel%", "%date%", "%time%", "%username%",
    "%userprofile%", "%computername%", "%userdomain%", "%os%",
    "%cd%", "%homepath%", "%homedrive%", "%temp%", "%tmp%",
    "%windir%", "%systemroot%", "%systemdrive%", "%programfiles%",
    "%programfiles(x86)%", "%appdata%", "%localappdata%",
    "%cmdcmdline%", "%cmdextversion%", "%path%", "%pathext%",
    "%prompt%", "%sessionname%", "%logonserver%",
    "%processor_architecture%", "%processor_identifier%",
    "%processor_level%", "%processor_revision%", "%number_of_processors%",
    "%psmodulepath%", "%public%", "%commonprogramfiles%",
    "%commonprogramfiles(x86)%", "%allusersprofile%",
    "%RANDOM%", "%ERRORLEVEL%", "%DATE%", "%TIME%", "%USERNAME%",
    "%CD%", "%PATH%", "%TEMP%", "%TMP%"
}

SYSTEM_VAR_NAMES = {v.strip('%').lower() for v in SYSTEM_VARIABLES}


class TokenType(Enum):
    LABEL = "label"
    SYSTEM_VAR = "system_var"
    USER_VAR = "user_var"
    FOR_VAR = "for_var"
    ARG_VAR = "arg_var"
    VARREF = "varref"
    DELAYED_SYSTEM = "delayed_system"
    DELAYED_USER = "delayed_user"
    DELAYED = "delayed"
    OPERATOR = "op"
    TEXT = "text"


@dataclass
class Token:
    type: TokenType
    text: str
    is_protected: bool = False

    def __repr__(self) -> str:
        return f"Token({self.type.value}: {self.text!r})"


class ObfuscationError(Exception):
    pass


class InvalidFileError(ObfuscationError):
    pass


class MappingError(ObfuscationError):
    pass


def extract_user_variables(content: str) -> Set[str]:
    user_vars: Set[str] = set()
    
    for line in content.splitlines():
        stripped = line.strip()
        
        match = SET_ASSIGN_PATTERN.match(stripped)
        if match:
            var_name = match.group(1).lower()
            if var_name not in SYSTEM_VAR_NAMES and not var_name.startswith('_'):
                user_vars.add(var_name)
            continue
        
        match = SET_PATTERN.match(stripped)
        if match:
            var_name = match.group(1).lower()
            if var_name not in SYSTEM_VAR_NAMES and not var_name.startswith('_'):
                if '=' in stripped:
                    user_vars.add(var_name)
    
    for_match = re.compile(r'for\s+%%.*?in\s*\(.*?\)\s+do\s+set\s+["\']?([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE)
    for line in content.splitlines():
        match = for_match.search(line)
        if match:
            var_name = match.group(1).lower()
            if var_name not in SYSTEM_VAR_NAMES:
                user_vars.add(var_name)
    
    setlocal_pattern = re.compile(r'^\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=', re.IGNORECASE)
    for line in content.splitlines():
        match = setlocal_pattern.match(line.strip())
        if match:
            var_name = match.group(1).lower()
            if var_name not in SYSTEM_VAR_NAMES:
                user_vars.add(var_name)
    
    logger.debug(f"Detected user variables: {user_vars}")
    return user_vars


def clean_comments(content: str) -> str:
    out_lines: List[str] = []
    for ln in content.splitlines():
        stripped = ln.lstrip()
        low = stripped.lower()
        if low.startswith("rem ") or low == "rem" or low.startswith("::"):
            continue
        out_lines.append(ln)
    logger.debug(f"Cleaned comments: {len(content.splitlines())} -> {len(out_lines)} lines")
    return "\n".join(out_lines)


def generate_substrings(num_keys: int = NUM_KEYS, alphabet: List[str] | None = None) -> Dict[str, str]:
    if alphabet is None:
        alphabet = ALPHABET.copy()
    if num_keys <= 0:
        raise MappingError(f"num_keys must be positive, got {num_keys}")
    if num_keys > 100:
        raise MappingError(f"num_keys too large: {num_keys} (max 100)")
    mapping: Dict[str, str] = {}
    for i in range(num_keys):
        pool = alphabet.copy()
        random.shuffle(pool)
        mapping[f"{KEY_PREFIX}{i}"] = "".join(pool)
    logger.debug(f"Generated {num_keys} variable mappings")
    return mapping


def is_system_variable(var_text: str) -> bool:
    return var_text.lower() in {v.lower() for v in SYSTEM_VARIABLES}


def is_user_variable(var_text: str, user_vars: Set[str]) -> bool:
    var_name = var_text.strip('%!').lower()
    return var_name in user_vars


def is_for_variable(var_text: str) -> bool:
    return bool(re.match(r'^%%[a-zA-Z]$', var_text))


def is_arg_variable(var_text: str) -> bool:
    return bool(re.match(r'^%\d+$', var_text))


def tokenize_line(line: str, user_vars: Set[str]) -> List[Token]:
    if LABEL_PATTERN.match(line):
        return [Token(TokenType.LABEL, line, is_protected=True)]
    
    tokens: List[Token] = []
    i = 0
    
    while i < len(line):
        matched = False
        
        for op in OPERATORS:
            if line.startswith(op, i):
                tokens.append(Token(TokenType.OPERATOR, op, is_protected=True))
                i += len(op)
                matched = True
                break
        
        if matched:
            continue
        
        ch = line[i]
        
        if ch == "%":
            if i + 2 < len(line) and line[i:i+2] == "%%" and line[i+2].isalpha():
                tokens.append(Token(TokenType.FOR_VAR, line[i:i+3], is_protected=True))
                i += 3
                continue
            
            j = i + 1
            while j < len(line) and line[j] != "%":
                j += 1
            
            if j < len(line):
                var_text = line[i:j+1]
                if is_system_variable(var_text) or is_arg_variable(var_text):
                    tokens.append(Token(TokenType.SYSTEM_VAR, var_text, is_protected=True))
                elif is_user_variable(var_text, user_vars):
                    tokens.append(Token(TokenType.USER_VAR, var_text, is_protected=True))
                else:
                    tokens.append(Token(TokenType.VARREF, var_text, is_protected=False))
                i = j + 1
                continue
            else:
                tokens.append(Token(TokenType.TEXT, ch, is_protected=False))
                i += 1
                continue
        
        if ch == "!":
            j = i + 1
            while j < len(line) and line[j] != "!":
                j += 1
            
            if j < len(line):
                var_text = line[i:j+1]
                inner_var = var_text[1:-1].lower()
                if inner_var in {v[1:-1].lower() for v in SYSTEM_VARIABLES}:
                    tokens.append(Token(TokenType.DELAYED_SYSTEM, var_text, is_protected=True))
                elif inner_var in user_vars:
                    tokens.append(Token(TokenType.DELAYED_USER, var_text, is_protected=True))
                else:
                    tokens.append(Token(TokenType.DELAYED, var_text, is_protected=False))
                i = j + 1
                continue
            else:
                tokens.append(Token(TokenType.TEXT, ch, is_protected=False))
                i += 1
                continue
        
        if ch == "^" and i + 1 < len(line):
            tokens.append(Token(TokenType.TEXT, line[i:i+2], is_protected=False))
            i += 2
            continue
        
        if ch == '"':
            j = i + 1
            while j < len(line) and line[j] != '"':
                j += 1
            if j < len(line):
                tokens.append(Token(TokenType.TEXT, line[i:j+1], is_protected=True))
                i = j + 1
                continue
        
        j = i + 1
        while j < len(line):
            if line[j] in "%!" or line[j] == '^' or line[j] == '"' or any(line.startswith(op, j) for op in OPERATORS):
                break
            j += 1
        
        tokens.append(Token(TokenType.TEXT, line[i:j], is_protected=False))
        i = j
    
    return tokens


def obfuscate_text(text: str, keys: List[str], values: List[str]) -> str:
    buf: List[str] = []
    for ch in text:
        candidates = []
        for idx, val in enumerate(values):
            pos = val.find(ch)
            if pos != -1:
                candidates.append((idx, pos))
        
        if candidates:
            idx, pos = random.choice(candidates)
            buf.append(f"%{keys[idx]}:~{pos},1%")
        else:
            buf.append(ch)
    return "".join(buf)


def obfuscate_with_substrings(content: str, mapping: Dict[str, str], user_vars: Set[str]) -> str:
    keys = list(mapping.keys())
    values = [mapping[k] for k in keys]
    out_lines: List[str] = []
    
    for line in content.splitlines():
        if LABEL_PATTERN.match(line):
            out_lines.append(line)
            continue
        
        tokens = tokenize_line(line, user_vars)
        new_parts: List[str] = []
        
        for token in tokens:
            if token.is_protected:
                new_parts.append(token.text)
            elif token.type == TokenType.VARREF:
                inner_text = token.text[1:-1]
                obfuscated_inner = obfuscate_text(inner_text, keys, values)
                new_parts.append(f"%{obfuscated_inner}%")
            elif token.type == TokenType.DELAYED:
                inner_text = token.text[1:-1]
                obfuscated_inner = obfuscate_text(inner_text, keys, values)
                new_parts.append(f"!{obfuscated_inner}!")
            else:
                new_parts.append(obfuscate_text(token.text, keys, values))
        
        out_lines.append("".join(new_parts))
    
    logger.debug(f"Obfuscated {len(content.splitlines())} lines")
    return "\n".join(out_lines)


def generate_set_lines(mapping: Dict[str, str]) -> List[str]:
    return [f'set "{k}={v}"' for k, v in mapping.items()]


def backup_if_exists(path: Path) -> None:
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup)
            logger.info(f"Created backup: {backup}")
        except IOError as e:
            logger.warning(f"Failed to create backup: {e}")


def process_file(path: Path, num_keys: int = NUM_KEYS, verbose: bool = False) -> Path:
    if not path.exists():
        raise InvalidFileError(f"File not found: {path}")
    if not path.is_file():
        raise InvalidFileError(f"Not a file: {path}")
    if path.suffix.lower() != ".bat":
        logger.warning(f"File extension is {path.suffix}, expected .bat")
    
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise InvalidFileError(f"Failed to read file: {e}")
    
    if verbose:
        logger.info(f"Processing: {path}")
        logger.info(f"File size: {len(raw)} bytes")
    
    cleaned = clean_comments(raw)
    user_vars = extract_user_variables(cleaned)
    mapping = generate_substrings(num_keys=num_keys)
    set_lines = generate_set_lines(mapping)
    body = obfuscate_with_substrings(cleaned, mapping, user_vars)
    
    out = "@echo off\n" + "\n".join(set_lines) + "\n" + body + "\n"
    out_path = path.with_name(path.stem + "-obf" + path.suffix)
    
    backup_if_exists(out_path)
    
    try:
        out_path.write_text(out, encoding="utf-8")
        logger.info(f"Obfuscated output written to: {out_path}")
    except Exception as e:
        raise InvalidFileError(f"Failed to write output: {e}")
    
    if verbose:
        logger.info(f"Output size: {len(out)} bytes")
        logger.info(f"Compression ratio: {len(out)/len(raw):.2%}")
        logger.info(f"Protected user variables: {user_vars}")
    
    return out_path


def cli() -> int:
    p = argparse.ArgumentParser(
        description="Batch (.bat) obfuscator with auto variable detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s script.bat
  %(prog)s script.bat --keys 10
  %(prog)s script.bat -v
        """
    )
    p.add_argument("file", type=Path, help="Path to .bat file")
    p.add_argument("--keys", type=int, default=NUM_KEYS, help=f"Number of VAR keys to generate (default: {NUM_KEYS})")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    
    args = p.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    try:
        out = process_file(args.file, num_keys=args.keys, verbose=args.verbose)
        print(f"Obfuscated batch written to: {out}")
        return 0
    except InvalidFileError as e:
        print(f"Invalid file: {e}", file=sys.stderr)
        return 1
    except MappingError as e:
        print(f"Mapping error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        logger.exception("Unexpected error:")
        return 2


if __name__ == "__main__":
    sys.exit(cli())
