import dis

from ..java import (
    Code as JavaCode,
    opcodes as JavaOpcodes,
    ExceptionInfo as JavaExceptionInfo,
)

from .utils import extract_command
from .opcodes import ASTORE_name, ALOAD_name, IF, END_IF


class IgnoreBlock(Exception):
    """An escape hatch; enable a block to be flagged as ignorable"""
    pass


class CodeParts:
    def __init__(self, context):
        self.context = context
        self.code = []
        self.try_catches = []
        self.if_blocks = []

        self.next_resolve_list = []

    def add_opcodes(self, *opcodes):
        self.code.extend(opcodes)
        for (obj, attr) in self.next_resolve_list:
            # print("        resolve %s reference on %s with %s" % (attr, id(obj), opcodes[0]))
            setattr(obj, attr, opcodes[0])
            opcodes[0].references.append((obj, attr))
        self.next_resolve_list = []

    def tweak(self):
        self.code = self.context.tweak(self.code)

    def stack_depth(self):
        "Evaluate the maximum stack depth required by a sequence of Java opcodes"
        depth = 0
        max_depth = 0
        for opcode in self.code:
            # print("   ", opcode)
            depth = depth + opcode.stack_effect
            if depth > max_depth:
                max_depth = depth
        return max_depth


class Block:
    def __init__(self, parent=None, commands=None):
        self.parent = parent
        self.commands = commands if commands else []
        self.localvars = {}

    def store_name(self, name, arguments):
        return [
            ASTORE_name(self.localvars, self.name)
        ]

    def load_name(self, name, arguments):
        code = []
        try:
            # Look for a local first.
            code.append(ALOAD_name(self.localvars, self.name))
        except KeyError:
            code.extend([
                # If there isn't a local, look for a global
                JavaOpcodes.GETSTATIC(self.module.descriptor, 'globals', 'Lorg/python/Object;'),
                JavaOpcodes.LDC(self.name),
                JavaOpcodes.INVOKEVIRTUAL('java/util/Hashtable', 'get', '(Ljava/lang/String;)Ljava/lang/Object;'),

                # If there's nothing in the globals, then look for a builtin.
                IF(
                    [JavaOpcodes.DUP()],
                    JavaOpcodes.IFNONNULL
                ),
                    JavaOpcodes.POP(),
                    JavaOpcodes.GETSTATIC('org/Python', 'builtins', 'Lorg/python/Object;'),
                    JavaOpcodes.LDC(self.name),
                    JavaOpcodes.INVOKEVIRTUAL('java/util/Hashtable', 'get', '(Ljava/lang/String;)Ljava/lang/Object;'),
                END_IF()
            ])

        return code

    @property
    def is_module(self):
        return False

    def extract(self, code):
        """Break a code object into the parts it defines, populating the
        provided block.

        """
        instructions = list(dis.Bytecode(code))
        i = len(instructions)
        commands = []
        while i > 0:
            i, command = extract_command(instructions, i)
            commands.append(command)

        commands.reverse()

        print ('=====' * 10)
        print (code)
        print ('-----' * 10)
        for command in commands:
            command.dump()
        print ('=====' * 10)

        # Append the extracted commands to any pre-existing ones.
        self.commands.extend(commands)

    def tweak(self, code):
        """Tweak the bytecode generated for this block."""
        return code

    def ignore_empty(self, code):
        if len(code) == 1 and isinstance(code[0], JavaOpcodes.RETURN):
            raise IgnoreBlock()
        elif len(code) == 2 and isinstance(code[1], JavaOpcodes.ARETURN):
            raise IgnoreBlock()
        return code

    def void_return(self, code):
        """Ensure that end of the code sequence is a Java-style return of void.

        Java has a separate opcode for VOID returns, which is different to
        RETURN NULL. Replace "SET NULL" "ARETURN" pair with "RETURN".
        """

        if len(code) >= 2 and isinstance(code[-2], JavaOpcodes.ACONST_NULL) and isinstance(code[-1], JavaOpcodes.ARETURN):
            # There might be opcodes referencing these two - in particular if
            # the last thing in the function is the end of an IF or TRY block.
            # Find all the blocks that reference these two opcodes and update
            # the references.
            return_opcode = JavaOpcodes.RETURN()
            for obj, attr in code[-2].references:
                setattr(obj, attr, return_opcode)
            for obj, attr in code[-1].references:
                setattr(obj, attr, return_opcode)
            code = code[:-2] + [return_opcode]
        return code

    def transpile(self):
        """Create a JavaCode object representing the commands stored in the block

        May raise ``IgnoreBlock`` if the block should be ignored.
        """
        # Convert the sequence of commands into instructions.
        # Most of the instructions will be opcodes. However, some will
        # be instructions to add exception blocks, line number references,
        # or other

        parts = CodeParts(self)
        for cmd in self.commands:
            for instruction in cmd.operation.convert(self, cmd.arguments):
                instruction.process(parts)

        # Java requires that every body of code finishes with a return.
        # Make sure there is one.
        if not isinstance(parts.code[-1], (JavaOpcodes.RETURN, JavaOpcodes.ARETURN)):
            parts.add_opcodes(JavaOpcodes.RETURN())

        # Provide any tweaks that are needed because of the context in which
        # the block is being used.
        parts.tweak()

        # Now that we have a complete opcode list, postprocess the list
        # with the known offsets.
        offset = 0
        for index, instruction in enumerate(parts.code):
            instruction.code_index = index
            instruction.code_offset = offset
            offset += len(instruction)

        # Then construct the exception table, updating any
        # end-of-exception GOTO operations with the right opcode.
        # Record a frame range for each one.
        exceptions = []
        for try_catch in parts.try_catches:
            # print("TRY CATCH START", id(try_catch), try_catch.start_op, try_catch.start_op.code_offset)
            # print("            END", try_catch.end_op)
            for handler in try_catch.handlers:
                exceptions.append(JavaExceptionInfo(
                    try_catch.start_op.code_offset,
                    try_catch.jump_op.code_offset,
                    handler.start_op.code_offset,
                    handler.descriptor
                ))

            try_catch.jump_op.offset = try_catch.end_op.code_offset - try_catch.jump_op.code_offset

        # Lastly, update any IF-related offsets
        for if_block in parts.if_blocks:
            # print ("IF BLOCK START", id(if_block), if_block.if_op, if_block.if_op.code_offset)
            # print ("         END", if_block.end_op, if_block.end_op.code_offset)
            # print ("IF BLOCK JUMP", if_block.jump_op, if_block.jump_op.code_offset)
            # Update the jumps for the initial IF block
            if_block.if_op.offset = if_block.end_op.code_offset - if_block.if_op.code_offset
            if if_block.jump_op:
                if_block.jump_op.offset = if_block.end_op.code_offset - if_block.jump_op.code_offset

            # # Update the jumps for each ELIF/ELSE
            for else_if in if_block.elifs:
                # print('    has elif')
                else_if.if_op.offset = if_block.end_op.code_offset - else_if.if_op.code_offset
                if else_if.jump_op:
                    else_if.jump_op.offset = if_block.end_op.code_offset - else_if.jump_op.code_offset

        return JavaCode(
            max_stack=parts.stack_depth(),
            max_locals=len(self.localvars),
            code=parts.code,
            exceptions=exceptions,
        )
