import argparse
import llvmlite.ir as ll
import llvmlite.binding as llvm
import sys

from ctypes import CFUNCTYPE, c_int8, c_void_p, cast

try:
    import faulthandler
    faulthandler.enable()
except ImportError:
    pass


llvm.initialize()
llvm.initialize_native_target()
llvm.initialize_native_asmprinter()


i8 = ll.IntType(8)
i32 = ll.IntType(32)
i64 = ll.IntType(64)

MEMORY_SIZE = 1 << 16

COMPILED_FN_TYPE = ll.FunctionType(ll.VoidType(), [])
MEMORY_TYPE = ll.ArrayType(i8, MEMORY_SIZE)

ZERO_i8 = ll.Constant(i8, 0)
ZERO_i32 = ll.Constant(i8, 0)
ONE_i8 = ll.Constant(i8, 1)
ONE_i32 = ll.Constant(i32, 1)
MEMORY_MASK_i32 = ll.Constant(i32, MEMORY_SIZE - 1)


def getc():
    return sys.stdin.read(1)


def putc(c):
    sys.stdout.write(chr(c & 0x7f))
    sys.stdout.flush()


# These need to be GLOBAL, otherwise the GC will clean it up, making it
# uncallable from the JIT.
PUTC_WRAPPER = CFUNCTYPE(None, c_int8)(putc)
GETC_WRAPPER = CFUNCTYPE(c_int8)(getc)


def compile_bf(prog):
    """
    Compiles the given BF program text into a llvm.ModuleRef object.

    The module will contain 3 globals:
        - the main function, void bfmain()
        - the memory, int8_t memory[]
        - the memory index, int32_t index
    """
    module = ll.Module()
    func = ll.Function(module, ll.FunctionType(ll.VoidType(), []), 'bfmain')
    builder = ll.IRBuilder()
    g_memory = ll.GlobalVariable(module, MEMORY_TYPE, 'memory')
    g_index = ll.GlobalVariable(module, i32, 'index')

    # Initialize the memory and index pointer to 0.
    g_memory.initializer = ll.Constant(MEMORY_TYPE, None)
    g_index.initializer = ll.Constant(i32, None)

    # Create the "putc" function.
    putc_type = ll.FunctionType(ll.VoidType(), [i8])
    getc_type = ll.FunctionType(i8, [])
    f_putc = create_thunk(module, putc_type, PUTC_WRAPPER, 'putc')
    f_getc = create_thunk(module, getc_type, GETC_WRAPPER, 'getc')

    # The block_stack tracks the current block and remaining blocks. The top
    # block is what we currently compile into, the one below it is the block
    # that follows the next ] (if we're in a loop).
    block_stack = [func.append_basic_block('entry')]
    loop_stack = []
    builder.position_at_end(block_stack[-1])

    def current_index_ptr():
        index = builder.load(g_index)
        # The extra dereference here with ZERO is required because g_memory is
        # itself a pointer, so this becomes &g_memory[0][index]. Yeah, it's
        # weird.
        # Ref: https://llvm.org/docs/GetElementPtr.html
        return builder.gep(g_memory, [ZERO_i32, index])

    line = 1
    col = 1
    for ch in prog:
        col += 1
        if ch == '\n':
            col = 1
            line += 1

        elif ch == '.':
            ptr = current_index_ptr()
            value = builder.load(ptr)
            builder.call(f_putc, [value])

        elif ch == ',':
            res = builder.call(f_getc, [])
            ptr = current_index_ptr()
            builder.store(res, ptr)

        elif ch == '>':
            # builder.call(f_putc, [ll.Constant(i8, ord('>'))])
            index = builder.load(g_index)
            index = builder.add(index, ONE_i32)
            index = builder.and_(index, MEMORY_MASK_i32)
            builder.store(index, g_index)

        elif ch == '<':
            # builder.call(f_putc, [ll.Constant(i8, ord('<'))])
            index = builder.load(g_index)
            index = builder.sub(index, ONE_i32)
            index = builder.and_(index, MEMORY_MASK_i32)
            builder.store(index, g_index)

        elif ch == '+':
            # builder.call(f_putc, [ll.Constant(i8, ord('+'))])
            ptr = current_index_ptr()
            value = builder.load(ptr)
            value = builder.add(value, ONE_i8)
            builder.store(value, ptr)

        elif ch == '-':
            # builder.call(f_putc, [ll.Constant(i8, ord('-'))])
            ptr = current_index_ptr()
            value = builder.load(ptr)
            value = builder.sub(value, ONE_i8)
            builder.store(value, ptr)

        elif ch == '[':  # start a loop
            # builder.call(f_putc, [ll.Constant(i8, ord('['))])
            loop_block = func.append_basic_block()
            tail_block = func.append_basic_block()

            # If memory[index] != 0, enter loop, otherwise skip.
            ptr = current_index_ptr()
            value = builder.load(ptr)
            nonzero = builder.icmp_unsigned('!=', value, ZERO_i8)
            builder.cbranch(nonzero, loop_block, tail_block)

            # Update our block stack. The current block is finished.
            block_stack.pop()
            block_stack.append(tail_block)
            block_stack.append(loop_block)
            loop_stack.append(loop_block)
            builder.position_at_end(block_stack[-1])

        elif ch == ']':  # end a loop
            # builder.call(f_putc, [ll.Constant(i8, ord(']'))])
            if len(block_stack) <= 1:
                raise ValueError('{}:{}: unmatched ]'.format(line, col))

            # If memory[index] != 0, repeat current loop, otherwise break.
            ptr = current_index_ptr()
            value = builder.load(ptr)
            nonzero = builder.icmp_unsigned('!=', value, ZERO_i8)
            builder.cbranch(nonzero, loop_stack[-1], block_stack[-2])

            # Update our block stack. The current block is finished.
            block_stack.pop()
            loop_stack.pop()
            builder.position_at_end(block_stack[-1])

        else:
            continue

    if len(block_stack) != 1:
        raise ValueError('{}:{}: unmatched ['.format(line, col))

    # Finish the function.
    builder.ret_void()

    assembly = str(module)
    # print(assembly)

    return llvm.parse_assembly(assembly)


def create_thunk(module, func_type, c_wrapper, name):
    """
    Creates a function in the given module that thunks back into Python.

    Returns a reference to the created function.
    """
    thunk_addr = ll.Constant(i64, cast(c_wrapper, c_void_p).value)
    func_ptr_type = func_type.as_pointer()

    func = ll.Function(module, func_type, name=name)
    builder = ll.IRBuilder(func.append_basic_block('entry'))

    thunk = builder.inttoptr(thunk_addr, func_ptr_type)
    call = builder.call(thunk, func.args)

    if isinstance(func_type.return_type, ll.VoidType):
        builder.ret_void()
    else:
        builder.ret(call)

    return func


def main():
    parser = argparse.ArgumentParser(description='BF Jit Compiler in Python, using LLVM')
    parser.add_argument('file', help='BF file to parse')
    parser.add_argument('-O', '--opt-level', type=int, default=3, help='Optimization level (0-3)')
    parser.add_argument('-s', '--asm', action='store_true', help='If provided, dump assembly instead of running')
    args = parser.parse_args()

    # Create a ModuleRef from our source code.
    with open(args.file, 'r') as f:
        prog = f.read()

    mod = compile_bf(prog)

    # Run optimizer on our module.
    pmb = llvm.create_pass_manager_builder()
    pmb.opt_level = args.opt_level
    pm = llvm.create_module_pass_manager()
    pmb.populate(pm)
    pm.run(mod)

    # Get target machine info.
    target = llvm.Target.from_default_triple().create_target_machine()

    # Make magic happen :)
    with llvm.create_mcjit_compiler(mod, target) as jit:
        jit.finalize_object()

        if args.asm:
            print(target.emit_assembly(mod))
        else:
            bfmain = CFUNCTYPE(None)(jit.get_function_address('bfmain'))
            bfmain()


if __name__ == '__main__':
    # print(getc())
    main()
