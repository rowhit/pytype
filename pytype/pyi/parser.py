"""Fast PYI parser."""

import collections
import hashlib

from pytype.pyi import parser_ext
from pytype.pytd import pep484
from pytype.pytd import pytd
from pytype.pytd.parse import visitors

_DEFAULT_VERSION = (2, 7, 6)
_DEFAULT_PLATFORM = "linux"


_Params = collections.namedtuple("_", ["required",
                                       "starargs", "starstarargs",
                                       "has_bare_star"])

_NameAndSig = collections.namedtuple("_", ["name", "signature",
                                           "decorator", "external_code"])


_COMPARES = {
    "==": lambda x, y: x == y,
    "!=": lambda x, y: x != y,
    "<": lambda x, y: x < y,
    "<=": lambda x, y: x <= y,
    ">": lambda x, y: x > y,
    ">=": lambda x, y: x >= y,
}


class _ConditionScope(object):
  """State associated with a condition if/elif/else block."""

  def __init__(self, parent):
    self._parent = parent
    if parent is None:
      self._active = True
      # The value of _can_trigger doesn't really matter since apply_condition
      # shouldn't be called on the top scope.
      self._can_trigger = False
    else:
      # By default new scopes are inactive and can be triggered iff the
      # parent is active.
      self._active = False
      self._can_trigger = parent.active

  def apply_condition(self, value):
    """Apply the value to this scope.

    If the scope can be triggered and value is true, then the scope
    becomes active, otherwise the scope is not active.  Note that a scope
    can trigger at most once since triggering also clears _can_trigger.

    Args:
      value: a bool.
    """
    assert self._parent is not None
    if self._can_trigger and value:
      self._active = True
      self._can_trigger = False
    else:
      self._active = False

  @property
  def active(self):
    return self._active

  @property
  def parent(self):
    return self._parent


class ParseError(Exception):

  """Exceptions raised by the parser."""

  def __init__(self, msg, line=None, filename=None, column=None, text=None):
    super(ParseError, self).__init__(msg)
    self._line = line
    self._filename = filename
    self._column = column
    self._text = text

  @property
  def line(self):
    return self._line

  def __str__(self):
    lines = []
    if self._filename or self._line is not None:
      lines.append('  File: "%s", line %s' % (self._filename, self._line))
    if self._column and self._text:
      indent = 4
      stripped = self._text.lstrip()
      lines.append("%*s%s" % (indent, "", stripped))
      # Output a pointer below the error column, adjusting for stripped spaces.
      pos = indent + (self._column - 1) - (len(self._text) - len(stripped))
      lines.append("%*s^" % (pos, ""))
    lines.append("%s: %s" % (type(self).__name__, self.message))
    return "\n".join(lines)


class _Mutator(visitors.Visitor):
  """Visitor for changing parameters to BeforeAfterType instances.

  We model
    def f(x: old_type):
      x := new_type
  as
    def f(x: BeforeAfterType(old_type, new_type))
  .
  This visitor applies the body "x := new_type" to the function signature.
  """

  def __init__(self, name, new_type):
    super(_Mutator, self).__init__()
    self.name = name
    self.new_type = new_type
    self.successful = False

  def VisitParameter(self, p):
    if p.name == self.name:
      self.successful = True
      if p.optional:
        raise NotImplementedError(
            "Argument %s can not be both mutable and optional" % p.name)
      return p.Replace(mutated_type=self.new_type)
    else:
      return p


class _InsertTypeParameters(visitors.Visitor):
  """Visitor for inserting TypeParameter instances."""

  def EnterTypeDeclUnit(self, node):
    self.type_params = {p.name: p for p in node.type_params}

  def LeaveTypeDeclUnit(self, node):
    self.type_params = None

  def VisitNamedType(self, node):
    if node.name in self.type_params:
      return self.type_params[node.name]
    else:
      return node


class _Parser(object):
  """A class used to parse a single PYI file.

  The PYI parser is split into two parts: a low level parser implemented in
  in C++, and the high level parser written in Python.

  The low level parser calls the lexer (also in C++) determines which
  reductions to make, and performs simple actions such as building up lists or
  strings.  It relies on a "peer" to perform more complicated actions
  associated with construction of the AST.

  This class is the high level parser, which invokes the low level parser and
  serves as the peer for AST construction.  Thus it is both the caller and
  callee of the low level parser.

  The low level parser expects the following interface in its peer.

  Attributes that return constant objects:
    ELLIPSIS
    PARSE_ERROR
    NOTHING
    ANYTHING
    BYTESTRING
    UNICODESTRING

  Methods used in AST construction:
    new_constant()
    add_alias_or_constant()
    add_import()
    new_type()
    new_union_type()
    new_function()
    new_external_function()
    new_named_tuple()
    regiser_class_name()
    add_class()
    add_type_var()
    if_begin()
    if_elif()
    if_else()
    if_end()

  Other methods:
    set_error_location()


  Error handling is a bit tricky because it is important to associate
  location information with errors, but undesireable to move location
  information around for every call between the low level parser and the
  peer.  As a compromise, when errors are detected (either by the low level
  parser or by the peer raising an exception), set_error_location() is called
  with current location information, then the the call to parse_ext.parse()
  raises an exception (either a ParseError or whatever else was raised by
  the peer in the first place).  The high level parser can thus save location
  information from set_error_location(), catch the exception raised by
  parse_ext.parse(), and raise a new exception that includes a location.

  Conditional pyi code (under an "if" statement) is handled similar to a
  preprocessor, discarding any statements under False conditions rather than
  representing the entire "if" tree in the AST.  This approach allows methods
  such as add_alias_or_constant() to have side effects provided that they
  first check to see if the enclosing scope is active.  There are four
  peer calls used to support conditions:

  if_begin(self, condition): This should be invoked after parsing the initial
      condition but before processing any enclosed definitions.  It establishes
      a new _ConditionScope based on the evaluation of condition.  It returns
      a bool indicating if the scope will now be active.

  if_elif(self, condition): This should be invoked after parsing the condition
      following an "elif", but before any subsequent definitions.  It evaluates
      the condition and changes the scope's state appropriately.  It returns
      a bool indicating if the scope will now be active.

  if_else(self): This should be invoked after parsing "else" but before any
      subsequent definitions.  The scope will become active if it hasn't
      triggered on any previous conditions.  It returns a bool indicating
      if the scope will now be active.

  if_end(self, clauses): This should be called at the end of the entire if
      statement where clauses is a list of (active, defs) pairs.  Active is
      the return value of the corresponding if_begin/if_elif/if_else call, and
      defs is a list of definitions within that block.  The function returns
      the list of defs that should be processed (i.e. the defs in the tuple
      where active was True, or [] if no such tuple is present).

  See _eval_condition for a description of conditions.
  """

  # Values for the parsing context.
  ELLIPSIS = object()  # Special object to signal ELLIPSIS as a parameter.
  PARSE_ERROR = ParseError  # The class object (not an instance of it).
  NOTHING = pytd.NothingType()
  ANYTHING = pytd.AnythingType()
  BYTESTRING = pytd.NamedType("bytes")
  UNICODESTRING = pytd.NamedType("unicode")

  # Attributes that all namedtuple instances have.
  _NAMEDTUPLE_MEMBERS = ("_asdict", "__dict__", "_fields", "__getnewargs__",
                         "__getstate__", "_make", "_replace", "__slots__")

  def __init__(self, version, platform):
    """Initialize the parser.

    Args:
      version: A version tuple.
      platform: A platform string.
    """
    self._used = False
    self._error_location = None
    self._version = _three_tuple(version or _DEFAULT_VERSION)
    self._platform = platform or _DEFAULT_PLATFORM
    self._filename = None
    self._ast_name = None
    # The condition stack, start with a default scope that will always be
    # active.
    self._current_condition = _ConditionScope(None)
    # These fields accumulate definitions that are used to build the
    # final TypeDeclUnit.
    self._constants = []
    self._aliases = []
    self._classes = []
    self._type_params = []
    self._module_path_map = {}
    self._generated_classes = collections.defaultdict(list)

  def parse(self, src, name, filename):
    """Parse a PYI file and return the corresponding AST.

    Note that parse() should be called exactly once per _Parser instance.  It
    holds aggregated state during parsing and is not designed to be reused.

    Args:
      src: The source text to parse.
      name: The name of the module to be created.
      filename: The name of the source file.

    Returns:
      A pytd.TypeDeclUnit() representing the parsed pyi.

    Raises:
      ParseError: If the PYI source could not be parsed.
    """
    # Ensure instances do not get reused.
    assert not self._used
    self._used = True

    self._filename = filename
    self._ast_name = name
    self._type_map = {}

    try:
      defs = parser_ext.parse(self, src)
      ast = self._build_type_decl_unit(defs)
    except ParseError as e:
      if self._error_location:
        line = self._error_location[0]
        try:
          text = src.splitlines()[line-1]
        except IndexError:
          text = None
        raise ParseError(e.message, line=line, filename=self._filename,
                         column=self._error_location[1], text=text)
      else:
        raise e

    ast = ast.Visit(_InsertTypeParameters())
    # TODO(kramm): This is in the wrong place- it should happen after resolving
    # local names, in load_pytd.
    ast = ast.Visit(pep484.ConvertTypingToNative(name))

    if name:
      ast = ast.Replace(name=name)
      ast = ast.Visit(visitors.AddNamePrefix())
    else:
      # If there's no unique name, hash the sourcecode.
      ast = ast.Replace(name=hashlib.md5(src).hexdigest())

    return ast

  def _build_type_decl_unit(self, defs):
    """Return a pytd.TypeDeclUnit for the given defs (plus parser state)."""
    # defs contains both constant and function definitions.
    constants, functions, aliases = _split_definitions(defs)
    assert not aliases  # We handle top-level aliases in add_alias_or_constant.
    constants.extend(self._constants)

    generated_classes = [x for class_list in self._generated_classes.values()
                         for x in class_list]

    classes = generated_classes + self._classes

    all_names = (list(set(f.name for f in functions)) +
                 [c.name for c in constants] +
                 [c.name for c in self._type_params] +
                 [c.name for c in classes] +
                 [c.name for c in self._aliases])
    duplicates = [name
                  for name, count in collections.Counter(all_names).items()
                  if count >= 2]
    if duplicates:
      raise ParseError(
          "Duplicate top-level identifier(s): " + ", ".join(duplicates))

    functions, properties = _merge_signatures(functions)
    if properties:
      prop_names = ", ".join(p.name for p in properties)
      raise ParseError(
          "Module-level functions with property decorators: " + prop_names)

    return pytd.TypeDeclUnit(name=None,
                             constants=tuple(constants),
                             type_params=tuple(self._type_params),
                             functions=tuple(functions),
                             classes=tuple(classes),
                             aliases=tuple(self._aliases))

  def set_error_location(self, location):
    """Record the location of the current error.

    Args:
      location: A tuple (first_line, first_column, last_line, last_column).
    """
    self._error_location = location

  def _eval_condition(self, condition):
    """Evaluate a condition and return a bool.

    Args:
      condition: A condition tuple of (left, op, right). If op is "or", then
      left and right are conditions. Otherwise, left is a name, op is one of
      the comparison strings in _COMPARES, and right is the expected value.

    Returns:
      The boolean result of evaluating the condition.

    Raises:
      ParseError: If the condition cannot be evaluated.
    """
    left, op, right = condition
    if op == "or":
      return self._eval_condition(left) or self._eval_condition(right)
    else:
      return self._eval_comparison(left, op, right)

  def _eval_comparison(self, ident, op, value):
    """Evaluate a comparison and return a bool.

    Args:
      ident: A tuple of a dotted name string and an optional __getitem__ key
        (int or slice).
      op: One of the comparison operator strings in _COMPARES.
      value: Either a string, an integer, or a tuple of integers.

    Returns:
      The boolean result of the comparison.

    Raises:
      ParseError: If the comparison cannot be evaluted.
    """
    name, key = ident
    if name == "sys.version_info":
      if key is None:
        key = slice(None, None, None)
      assert isinstance(key, (int, slice))
      if isinstance(key, int) and not isinstance(value, int):
        raise ParseError(
            "an element of sys.version_info must be compared to an integer")
      if isinstance(key, slice) and not _is_int_tuple(value):
        raise ParseError(
            "sys.version_info must be compared to a tuple of integers")
      try:
        actual = self._version[key]
      except IndexError as e:
        raise ParseError(e.message)
      if isinstance(key, slice):
        actual = _three_tuple(actual)
        value = _three_tuple(value)
    elif name == "sys.platform":
      if not isinstance(value, str):
        raise ParseError("sys.platform must be compared to a string")
      if op not in ["==", "!="]:
        raise ParseError("sys.platform must be compared using == or !=")
      actual = self._platform
    else:
      raise ParseError("Unsupported condition: '%s'." % name)
    return _COMPARES[op](actual, value)

  def if_begin(self, condition):
    """Begin an "if" statement using the specified condition."""
    self._current_condition = _ConditionScope(self._current_condition)
    self._current_condition.apply_condition(self._eval_condition(condition))
    return self._current_condition.active

  def if_elif(self, condition):
    """Start an "elif" clause using the specified condition."""
    self._current_condition.apply_condition(self._eval_condition(condition))
    return self._current_condition.active

  def if_else(self):
    """Start an "else" clause using the specified condition."""
    self._current_condition.apply_condition(True)
    return self._current_condition.active

  def if_end(self, clauses):
    """Finish an "if" statement given a list of (active, defs) clauses."""
    self._current_condition = self._current_condition.parent
    for cond_value, stmts in clauses:
      if cond_value:
        return stmts
    return []

  def new_constant(self, name, value):
    """Return a Constant.

    Args:
      name: The name of the constant.
      value: None, 0, or a  pytd type.

    Returns:
      A Constant object.

    Raises:
      ParseError: if value is an int other than 0.
    """
    if value is None:
      t = pytd.AnythingType()
    elif isinstance(value, int):
      if value != 0:
        raise ParseError("Only '0' allowed as int literal")
      t = pytd.NamedType("int")
    else:
      t = value
    return pytd.Constant(name, t)

  def new_alias_or_constant(self, name_and_value):
    name, value = name_and_value
    if value in [pytd.NamedType("True"), pytd.NamedType("False")]:
      return pytd.Constant(name, pytd.NamedType("bool"))
    else:
      return pytd.Alias(name, value)

  def add_alias_or_constant(self, name_and_value):
    """Add an alias or constant.

    Args:
      name_and_value: The name and value of the alias or constant.
    """
    if not self._current_condition.active:
      return
    # TODO(dbaum): Consider merging this with new_constant().
    alias_or_constant = self.new_alias_or_constant(name_and_value)
    if isinstance(alias_or_constant, pytd.Constant):
      self._constants.append(alias_or_constant)
    else:
      name, value = name_and_value
      self._type_map[name] = value
      self._aliases.append(alias_or_constant)

  def add_import(self, from_package, import_list):
    """Add an import.

    Args:
      from_package: A dotted package name if this is a "from" statement, or None
          if it is an "import" statement.
      import_list: A list of imported items, which are either strings or pairs
          of strings.  Pairs are used when items are renamed during import
          using "as".

    Raises:
      ParseError: If an import statement uses a rename.
    """
    if from_package:
      if not self._current_condition.active:
        return
      # from a.b.c import d, ...
      for item in import_list:
        if isinstance(item, tuple):
          name, new_name = item
        else:
          name = new_name = item
        if name != "*":
          t = pytd.NamedType("%s.%s" % (from_package, name))
          self._type_map[new_name] = t
          if from_package != "typing":
            self._aliases.append(pytd.Alias(new_name, t))
            self._module_path_map[name] = "%s.%s" % (from_package, name)
        else:
          pass  # TODO(kramm): Handle '*' imports in pyi
    else:
      # No need to check _current_condition since there are no side effects.
      # import a, b as c, ...
      for item in import_list:
        # simple import, no impact on pyi, but check for unsupported rename.
        if isinstance(item, tuple):
          raise ParseError(
              "Renaming of modules not supported. Use 'from' syntax.")

  def new_type(self, name, parameters=None):
    """Return the AST for a type.

    Args:
      name: The name of the type.
      parameters: List of type parameters.

    Returns:
      A pytd type node.

    Raises:
      ParseError: if parameters are not supplied for a base_type that requires
          parameters, such as Union.
    """
    base_type = self._type_map.get(name)
    if base_type is None:
      module, dot, tail = name.partition(".")
      full_name = self._module_path_map.get(module, module) + dot + tail
      base_type = pytd.NamedType(full_name)
    if parameters is not None:
      return self._parameterized_type(base_type, parameters)
    else:
      if (isinstance(base_type, pytd.NamedType) and
          base_type.name in ["typing.Union", "typing.Optional"]):
        raise ParseError("Missing options to %s" % base_type.name)
      return base_type

  def _is_tuple_base_type(self, t):
    return isinstance(t, pytd.NamedType) and (
        t.name == "tuple" or
        (self._ast_name != "__builtin__" and t.name == "__builtin__.tuple") or
        (self._ast_name == "typing" and t.name == "Tuple") or
        (self._ast_name != "typing" and t.name == "typing.Tuple"))

  def _is_callable_base_type(self, t):
    return isinstance(t, pytd.NamedType) and (
        (self._ast_name == "typing" and t.name == "Callable") or
        (self._ast_name != "typing" and t.name == "typing.Callable"))

  def _heterogeneous_tuple(self, base_type, parameters):
    if parameters:
      return pytd.TupleType(base_type=base_type, parameters=parameters)
    else:
      return pytd.GenericType(base_type=base_type,
                              parameters=(pytd.NothingType(),))

  def _is_empty_tuple(self, t):
    return (isinstance(t, pytd.GenericType) and
            self._is_tuple_base_type(t.base_type) and
            t.parameters == (pytd.NothingType(),))

  def _is_heterogeneous_tuple(self, t):
    # An empty tuple is represented as a GenericType rather than a TupleType,
    # but we still consider it heterogeneous because we know exactly what the
    # parameters are (there are none).
    return isinstance(t, pytd.TupleType) or self._is_empty_tuple(t)

  def _is_any(self, t):
    return isinstance(t, pytd.AnythingType) or t == pytd.NamedType("typing.Any")

  def _parameterized_type(self, base_type, parameters):
    """Return a parameterized type."""
    if len(parameters) == 2 and parameters[-1] is self.ELLIPSIS and (
        not self._is_callable_base_type(base_type)):
      element_type = parameters[0]
      if element_type is self.ELLIPSIS:
        raise ParseError("[..., ...] not supported")
      return pytd.GenericType(base_type=base_type,
                              parameters=(element_type,))
    else:
      parameters = tuple(pytd.AnythingType() if p is self.ELLIPSIS else p
                         for p in parameters)
      if self._is_tuple_base_type(base_type):
        return self._heterogeneous_tuple(base_type, parameters)
      elif (self._is_callable_base_type(base_type) and
            self._is_heterogeneous_tuple(parameters[0])):
        if len(parameters) > 2:
          raise ParseError(
              "Expected 2 parameters to Callable, got %d" % len(parameters))
        if len(parameters) == 1:
          # We're usually happy to treat omitted parameters as "Any", but we
          # need a return type for CallableType, or we wouldn't know whether the
          # last parameter is an argument or return type.
          parameters += (pytd.AnythingType(),)
        if self._is_empty_tuple(parameters[0]):
          parameters = parameters[1:]
        else:
          parameters = parameters[0].parameters + parameters[1:]
        return pytd.CallableType(base_type=base_type, parameters=parameters)
      else:
        assert parameters
        if (self._is_callable_base_type(base_type) and
            not self._is_any(parameters[0])):
          raise ParseError(
              "First argument to Callable must be a list of argument types")
        return pytd.GenericType(base_type=base_type, parameters=parameters)

  def new_union_type(self, types):
    """Return a new UnionType composed of the specified types."""
    # UnionType flattens any contained UnionType's.
    return pytd.UnionType(tuple(types))

  def new_function(self, decorators, name, param_list, return_type, body):
    """Return a _NameAndSig object for the function.

    Args:
      decorators: List of decorator names.
      name: Name of funciton.
      param_list: List of parameters, where a paremeter is either a tuple
        (name, type, default) or the ELLIPSIS special object.  See
        _validate_params for a more detailed description of allowed parameters.
      return_type: A pytd type object.
      body: ?

    Returns:
      A _NameAndSig object.

    Raises:
      ParseError: if any validity checks fail.
    """
    if name == "__init__" and isinstance(return_type, pytd.AnythingType):
      ret = pytd.NamedType("NoneType")
    else:
      ret = return_type
    params = _validate_params(param_list)

    exceptions = []
    mutators = []
    for stmt in body:
      if isinstance(stmt, pytd.Type):
        exceptions.append(stmt)  # raise stmt
        continue
      assert isinstance(stmt, tuple) and len(stmt) == 2, stmt
      mutators.append(_Mutator(stmt[0], stmt[1]))

    signature = pytd.Signature(params=tuple(params.required), return_type=ret,
                               starargs=params.starargs,
                               starstarargs=params.starstarargs,
                               exceptions=tuple(exceptions), template=())
    for mutator in mutators:
      try:
        signature = signature.Visit(mutator)
      except NotImplementedError as e:
        raise ParseError(e.message)
      if not mutator.successful:
        raise ParseError("No parameter named %s" % mutator.name)

    # Remove ignored decorators, raise ParseError for invalid decorators.
    decorators = [d for d in decorators if _keep_decorator(d)]
    # TODO(acaceres): if not inside a class, any decorator should be an error
    if len(decorators) > 1:
      raise ParseError("Too many decorators for %s" % name)
    decorator, = decorators if decorators else (None,)

    return _NameAndSig(name=name, signature=signature,
                       decorator=decorator,
                       external_code=False)

  def new_external_function(self, decorators, name):
    """Return a _NameAndSig for an external code function."""
    del decorators
    return _NameAndSig(
        name=name,
        # signature is for completeness - it's ignored
        signature=pytd.Signature(params=(),
                                 starargs=None, starstarargs=None,
                                 return_type=pytd.NothingType(),
                                 exceptions=(),
                                 template=()),
        decorator=None,
        external_code=True)

  def _namedtuple_new(self, name, fields):
    """Build a __new__ method for a namedtuple with the given fields.

    For a namedtuple defined as NamedTuple("_", [("foo", int), ("bar", str)]),
    generates the method
      def __new__(cls: Type[_T], foo: int, bar: str) -> _T: ...
    where _T is a TypeVar bounded by the class type.

    Args:
      name: The class name.
      fields: A list of (name, type) pairs representing the namedtuple fields.

    Returns:
      A _NameAndSig object for a __new__ method.
    """
    type_param = pytd.TypeParameter("_T" + name, bound=pytd.NamedType(name))
    self._type_params.append(type_param)
    cls_arg = (
        "cls", pytd.GenericType(pytd.NamedType("type"), (type_param,)), None)
    args = [cls_arg] + [(n, t, None) for n, t in fields]
    return self.new_function((), "__new__", args, type_param, ())

  def _namedtuple_init(self):
    """Build an __init__ method for a namedtuple.

    Builds a dummy __init__ that accepts any arguments. Needed because our
    model of __builtin__.tuple uses __init__.

    Returns:
      A _NameAndSig object for an __init__ method.
    """
    args = [(name, pytd.AnythingType(), None)
            for name in ("self", "*args", "**kwargs")]
    ret = pytd.NamedType("NoneType")
    return self.new_function((), "__init__", args, ret, ())

  def new_named_tuple(self, base_name, fields):
    """Return a type for a named tuple (implicitly generates a class).

    Args:
      base_name: The named tuple's name.
      fields: A list of (name, type) tuples.

    Returns:
      A NamedType() for the generated class that describes the named tuple.
    """
    # Handle previously defined NamedTuples with the same name
    prev_list = self._generated_classes[base_name]
    class_name = "~%s_%d" % (base_name, len(prev_list))
    class_parent = self._heterogeneous_tuple(pytd.NamedType("tuple"),
                                             tuple(t for _, t in fields))
    class_constants = tuple(pytd.Constant(n, t) for n, t in fields)
    # Since the user-defined fields are the only namedtuple attributes commonly
    # used, we define all the other attributes as Any for simplicity.
    class_constants += tuple(pytd.Constant(name, pytd.AnythingType())
                             for name in self._NAMEDTUPLE_MEMBERS)
    methods = _merge_method_signatures(
        [self._namedtuple_new(class_name, fields), self._namedtuple_init()])
    nt_class = pytd.Class(name=class_name,
                          metaclass=None,
                          parents=(class_parent,),
                          methods=tuple(methods),
                          constants=class_constants,
                          template=())

    self._generated_classes[base_name].append(nt_class)
    return pytd.NamedType(nt_class.name)

  def register_class_name(self, class_name):
    """Register a class name so that it can shadow aliases."""
    if not self._current_condition.active:
      return
    self._type_map[class_name] = pytd.NamedType(class_name)

  def add_class(self, class_name, parent_args, defs):
    """Add a class to the module.

    Args:
      class_name: The name of the class (a string).
      parent_args: A list of parent types and (keyword, value) tuples.
          Parent types must be instances of pytd.Type.  Keyword tuples must
          appear at the end of the list.  Currently the only supported keyword
          is 'metaclass'.
      defs: A list of constant (pytd.Constant) and function (_NameAndSig)
          definitions.

    Raises:
      ParseError: if defs contains duplicate names (excluding multiple
          definitions of a function, which is allowed).
    """
    # Process parent_args, extracting parents and possibly a metaclass.
    parents = []
    metaclass = None
    for i, p in enumerate(parent_args):
      if isinstance(p, pytd.Type):
        parents.append(p)
      else:
        keyword, value = p
        if i != len(parent_args) - 1:
          raise ParseError("metaclass must be last argument")
        if keyword != "metaclass":
          raise ParseError("Only 'metaclass' allowed as classdef kwarg")
        metaclass = value

    constants, methods, aliases = _split_definitions(defs)

    all_names = (list(set(f.name for f in methods)) +
                 [c.name for c in constants] +
                 [a.name for a in aliases])
    duplicates = [name
                  for name, count in collections.Counter(all_names).items()
                  if count >= 2]
    if duplicates:
      # TODO(kramm): raise a syntax error right when the identifier is defined.
      raise ParseError("Duplicate identifier(s): " + ", ".join(duplicates))

    # This check is performed after the above error checking so that errors
    # will be spotted even in non-active conditional code.
    if not self._current_condition.active:
      return

    if aliases:
      vals_dict = {val.name: val for val in constants + aliases}
      for val in aliases:
        name = val.name
        while isinstance(val, pytd.Alias):
          if (not isinstance(val.type, pytd.NamedType) or
              val.type.name not in vals_dict):
            raise ParseError(
                "Illegal value for alias %r. "
                "Value must be an attribute on the same class." % val.name)
          val = vals_dict[val.type.name]
        constants.append(pytd.Constant(name, val.type))

    # TODO(dbaum): Is NothingType even legal here?  The grammar accepts it but
    # perhaps it should be a ParseError.
    parents = [p for p in parents if not isinstance(p, pytd.NothingType)]
    methods, properties = _merge_signatures(methods)
    # Ensure that old style classes inherit from classobj.
    if not parents and class_name not in ["classobj", "object"]:
      parents = (pytd.NamedType("classobj"),)
    cls = pytd.Class(name=class_name, metaclass=metaclass,
                     parents=tuple(parents),
                     methods=tuple(methods),
                     constants=tuple(constants + properties),
                     template=())
    self._classes.append(cls)

  def add_type_var(self, name, name_arg, args):
    """Add a type variable, <name> = TypeVar(<name_arg>, <args>)."""
    if name != name_arg:
      raise ParseError("TypeVar name needs to be %r (not %r)" % (
          name_arg, name))
    # 'bound' is the only keyword argument we currently use.
    # TODO(rechen): We should enforce the PEP 484 guideline that
    # len(constraints) != 1. However, this guideline is currently violated
    # in typeshed (see https://github.com/python/typeshed/pull/806).
    constraints, named_args = args
    named_args = dict(named_args) if named_args else {}
    extra = set(named_args) - {"bound", "covariant", "contravariant"}
    if extra:
      raise ParseError("Unrecognized keyword(s): %s" % ", ".join(extra))
    if not self._current_condition.active:
      return
    self._type_params.append(pytd.TypeParameter(
        name=name,
        constraints=() if constraints is None else tuple(constraints),
        bound=named_args.get("bound")))


def parse_string(src, name=None, filename=None, python_version=None,
                 platform=None):
  return _Parser(version=python_version, platform=platform).parse(
      src, name, filename)


def join_types(types):
  """Combine a list of types into a union type, if needed.

  Leaves singular return values alone, or wraps a UnionType around them if there
  are multiple ones, or if there are no elements in the list (or only
  NothingType) return NothingType.

  Arguments:
    types: A list of types. This list might contain other UnionTypes. If
    so, they are flattened.

  Returns:
    A type that represents the union of the types passed in. Order is preserved.
  """
  queue = collections.deque(types)
  seen = set()
  new_types = []
  while queue:
    t = queue.popleft()
    if isinstance(t, pytd.UnionType):
      queue.extendleft(reversed(t.type_list))
    elif isinstance(t, pytd.NothingType):
      pass
    elif t not in seen:
      new_types.append(t)
      seen.add(t)

  if len(new_types) == 1:
    return new_types.pop()
  elif any(isinstance(t, pytd.AnythingType) for t in new_types):
    return pytd.AnythingType()
  elif new_types:
    return pytd.UnionType(tuple(new_types))  # tuple() to make unions hashable
  else:
    return pytd.NothingType()


def _is_property_decorator(decorator):
  # Property decorators are the only ones where dotted names are accepted.
  return decorator == "property" or "." in decorator


def _keep_decorator(decorator):
  """Return True iff the decorator requires processing."""
  if decorator in ["overload", "abstractmethod"]:
    # These are legal but ignored.
    return False
  elif (decorator in ["staticmethod", "classmethod"] or
        _is_property_decorator(decorator)):
    return True
  else:
    raise ParseError("Decorator %s not supported" % decorator)


def _validate_params(param_list):
  """Validate and convert a param_list.

  Validate and convert parameter tuples to a _Params object.  This performs
  a number of checks that are easier to do after parsing rather than
  incorporating them into the grammar itself.

  Parameters are specified as either ELLIPSIS objects or (name, type, default)
  tuples, where name is a string, type is a pytd type or None, and default
  is a string, number or None.

  (name, None, None): A required parameter with no type information.
  (name, type, None): A parameter of the specified type.
  (name, None, default): An optional parameter.  In some cases, type information
      is derived from default (see _type_for_default).
  (name, type, default): An optional parameter with type information.  If
      default is the string "None" then the parameter type is widened to include
      both the specified type and NoneType.

  (*, None, None): A bare * parameter.
  (*name, None, None): A *args style argument of type tuple.
  (*name, type, None): A *args style argument of type tuple[type].
  (**name, None, None): A **kwargs style argument of type dict.
  (**name, type, None): A **kwargs style argument of type dict[str, type].
  ELLIPSIS: Syntactic sugar that adds both *args and *kwargs parameters.

  Args:
    param_list: list of (name, type, default) tuples and ELLIPSIS objects.

  Returns:
    A _Params instance.

  Raises:
    ParseError: if special arguments are present in the wrong positions or
        combinations.
  """
  # TODO(kramm): Disallow "self" and "cls" as names for param (if it's not
  # the first parameter).

  params = []
  has_bare_star = False
  stararg = None
  starstararg = None

  for i, param in enumerate(param_list):
    is_last = i == len(param_list) - 1
    if param is _Parser.ELLIPSIS:
      if not is_last:
        raise ParseError("ellipsis (...) must be last parameter")
      if has_bare_star:
        raise ParseError("ellipsis (...) not compatible with bare *")
      # TODO(dbaum): Shouldn't we pass the existing paramter names to
      # InventStarArgParams()?  The legacy parser doesn't, so leaving the
      # code idenentical to legacy for now.
      stararg, starstararg = visitors.InventStarArgParams([])
      continue

    name, param_type, default = param
    if name.startswith("**"):
      # **kwargs
      if not is_last:
        raise ParseError("%s must be last parameter" % name)
      starstararg = _starstar_param(name[2:], param_type)
    elif name.startswith("*"):
      # *args or *
      if stararg or has_bare_star:
        raise ParseError("Unexpected second *")
      if name == "*" and is_last:
        raise ParseError("Named arguments must follow bare *")
      if name == "*":
        has_bare_star = True
      else:
        stararg = _star_param(name[1:], param_type)
    else:
      kwonly = bool(stararg or has_bare_star)
      params.append(_normal_param(name, param_type, default, kwonly))

  return _Params(params,
                 stararg,
                 starstararg,
                 has_bare_star=has_bare_star)


def _normal_param(name, param_type, default, kwonly):
  """Return a pytd.Parameter object for a normal argument."""
  if default is not None:
    default_type = _type_for_default(default)
    if default_type == pytd.NamedType("NoneType"):
      if param_type is not None:
        param_type = pytd.UnionType((param_type, default_type))
    elif param_type is None:
      param_type = default_type
  if param_type is None:
    # TODO(kramm): We should use __builtin__.object. (And other places)
    param_type = pytd.NamedType("object")

  optional = default is not None
  return pytd.Parameter(name, param_type, kwonly, optional, None)


def _star_param(name, param_type):
  """Return a pytd.Parameter for a *args argument."""
  if param_type is None:
    param_type = pytd.NamedType("tuple")
  else:
    param_type = pytd.GenericType(
        pytd.NamedType("tuple"), (param_type,))
  return pytd.Parameter(name, param_type, False, True, None)


def _starstar_param(name, param_type):
  """Return a pytd.Parameter for a **kwargs argument."""
  if param_type is None:
    param_type = pytd.NamedType("dict")
  else:
    param_type = pytd.GenericType(
        pytd.NamedType("dict"), (pytd.NamedType("str"), param_type))
  return pytd.Parameter(name, param_type, False, True, None)


def _type_for_default(default):
  """Return a pytd type object for the given default value."""
  # TODO(kramm): We should use __builtin__ types here. (And other places)
  if default == "None":
    return pytd.NamedType("NoneType")
  elif isinstance(default, float):
    return pytd.NamedType("float")
  elif isinstance(default, int):
    return pytd.NamedType("int")
  else:
    # ELLIPSIS or NAMEs other than None are treated as object.
    return pytd.NamedType("object")


def _split_definitions(defs):
  """Return [constants], [functions] given a mixed list of definitions."""
  constants = []
  functions = []
  aliases = []
  for d in defs:
    if isinstance(d, pytd.Constant):
      constants.append(d)
    elif isinstance(d, _NameAndSig):
      functions.append(d)
    elif isinstance(d, pytd.Alias):
      aliases.append(d)
    else:
      raise TypeError("Unexpected definition type %s", type(d))
  return constants, functions, aliases


def _is_int_tuple(value):
  """Return whether the value is a tuple of integers."""
  return isinstance(value, tuple) and all(isinstance(v, int) for v in value)


def _three_tuple(value):
  """Append zeros and slice to normalize the tuple to a three-tuple."""
  return (value + (0, 0))[:3]


def _split_methods_and_properties(signatures):
  methods = []
  properties = []
  for signature in signatures:
    if signature.decorator and _is_property_decorator(signature.decorator):
      properties.append(signature)
    else:
      methods.append(signature)
  return methods, properties


def _parse_signature_as_property(full_signature):
  """Parse a signature as a property getter, setter, or deleter.

  Checks that the signature matches one of {@property, @foo.getter, @foo.setter,
  @foo.deleter} and gets the property type if specified in the signature.

  Args:
    full_signature: _NameAndSig

  Returns:
    property_type: The property type, or None.

  Raises:
    ParseError: If the signature cannot be parsed as a property.
  """
  name, signature, decorator, _ = full_signature
  # TODO(acaceres): validate full_signature.external_code?
  num_params = len(signature.params)
  if decorator in ("property", name + ".getter") and num_params == 1:
    return signature.return_type
  elif decorator == name + ".setter" and num_params == 2 or (
      decorator == name + ".deleter" and num_params == 1):
    # Setters and deleters contain no information about the property type.
    return None
  raise ParseError("Unhandled decorator: %s" % decorator)


def _merge_property_signatures(signatures):
  name_to_property_types = collections.OrderedDict()
  for signature in signatures:
    if signature.name not in name_to_property_types:
      name_to_property_types[signature.name] = []
    property_type = _parse_signature_as_property(signature)
    if property_type:
      name_to_property_types[signature.name].append(property_type)
  return [
      pytd.Constant(
          name=name, type=join_types(types) if types else pytd.AnythingType())
      for name, types in name_to_property_types.items()]


def _merge_method_signatures(signatures):
  """Group the signatures by name, turning each group into a function."""
  name_to_signatures = collections.OrderedDict()
  name_to_decorator = {}
  # map from function name to a bool indicating whether the function has an
  # external definition
  name_to_external_code = {}
  for name, signature, decorator, external_code in signatures:
    if name not in name_to_signatures:
      name_to_signatures[name] = []
      name_to_decorator[name] = decorator
    if name_to_decorator[name] != decorator:
      raise ParseError(
          "Overloaded signatures for %s disagree on decorators" % name)
    if name in name_to_external_code:
      if external_code and name_to_external_code[name]:
        raise ParseError("Multiple PYTHONCODEs for %s" % name)
      elif external_code != name_to_external_code[name]:
        raise ParseError("Mixed pytd and PYTHONCODEs for %s" % name)
    else:
      name_to_external_code[name] = external_code
    name_to_signatures[name].append(signature)
  methods = []
  for name, signatures in name_to_signatures.items():
    decorator = name_to_decorator[name]
    if name == "__new__" or decorator == "staticmethod":
      kind = pytd.STATICMETHOD
    elif decorator == "classmethod":
      kind = pytd.CLASSMETHOD
    else:
      kind = pytd.METHOD
    if name_to_external_code[name]:
      methods.append(pytd.ExternalFunction(name, (), kind))
    else:
      methods.append(pytd.Function(name, tuple(signatures), kind))
  return methods


def _merge_signatures(signatures):
  method_sigs, property_sigs = _split_methods_and_properties(signatures)
  methods = _merge_method_signatures(method_sigs)
  constants = _merge_property_signatures(property_sigs)
  bad_names = {m.name for m in methods} & {c.name for c in constants}
  if bad_names:
    raise ParseError("Incompatible signatures for %s" % ", ".join(bad_names))
  return methods, constants
