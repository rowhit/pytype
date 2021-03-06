# -*- coding:utf-8; python-indent:2; indent-tabs-mode:nil -*-

# Copyright 2013 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Visitor(s) for walking ASTs."""

import collections
import itertools
import logging
import re


from pytype import utils
from pytype.pytd import pytd
from pytype.pytd.parse import parser_constants  # pylint: disable=g-importing-member


class ContainerError(Exception):
  pass


class SymbolLookupError(Exception):
  pass


# A convenient value for unchecked_node_classnames if a visitor wants to
# use unchecked nodes everywhere.
ALL_NODE_NAMES = type(
    "contains_everything",
    (),
    {"__contains__": lambda *args: True})()


class _NodeClassInfo(object):
  """Representation of a node class in the precondition graph."""

  def __init__(self, cls):
    self.cls = cls  # The class object.
    self.name = cls.__name__
    # The set of NodeClassInfo objects that may appear below this particular
    # type of node.  Initially empty, filled in by examining preconditions.
    self.outgoing = set()


def _FindNodeClasses():
  """Yields _NodeClassInfo objects for each node found in pytd."""
  for name in dir(pytd):
    value = getattr(pytd, name)
    if isinstance(value, type) and hasattr(value, "_CHECKER"):
      yield _NodeClassInfo(value)


_IGNORED_TYPENAMES = set(["str", "bool", "NoneType"])
_ancestor_map = None  # Memoized ancestors map.


def _GetAncestorMap():
  """Return a map of node class names to a set of ancestor class names."""

  global _ancestor_map
  if _ancestor_map is None:
    # Map from name to _NodeClassInfo.
    node_classes = {i.name: i for i in _FindNodeClasses()}

    # Update _NodeClassInfo.outgoing based on preconditions.
    for info in node_classes.values():
      for allowed in info.cls._CHECKER.allowed_types():  # pylint: disable=protected-access
        if isinstance(allowed, type):
          # All subclasses of the type are allowed.
          info.outgoing.update(
              [i for i in node_classes.values() if issubclass(i.cls, allowed)])
        elif allowed in node_classes:
          info.outgoing.add(node_classes[allowed])
        elif allowed not in _IGNORED_TYPENAMES:
          # This means preconditions list a typename that is unknown.  If it
          # is a node then make sure _FindNodeClasses() can discover it.  If it
          # is not a node, then add the typename to _IGNORED_TYPENAMES.
          raise AssertionError("Unknown precondition typename: %s", allowed)

    predecessors = utils.compute_predecessors(node_classes.values())
    # Convert predecessors keys and values to use names instead of info objects.
    _ancestor_map = {
        k.name: {n.name for n in v} for k, v in predecessors.items()}
  return _ancestor_map


class Visitor(object):
  """Base class for visitors.

  Each class inheriting from visitor SHOULD have a fixed set of methods,
  otherwise it might break the caching in this class.

  Attributes:
    visits_all_node_types: Whether the visitor can visit every node type.
    unchecked_node_names: Contains the names of node classes that are unchecked
      when constructing a new node from visited children.  This is useful
      if a visitor returns data in part or all of its walk that would violate
      node preconditions.
    enter_functions: A dictionary mapping node class names to the
      corresponding Enter functions.
    visit_functions: A dictionary mapping node class names to the
      corresponding Visit functions.
    leave_functions: A dictionary mapping node class names to the
      corresponding Leave functions.
    visit_class_names: A set of node class names that must be visited.  This is
      constructed based on the enter/visit/leave functions and precondition
      data about legal ASTs.  As an optimization, the visitor will only visit
      nodes under which some actionable node can appear.
  """
  visits_all_node_types = False
  unchecked_node_names = set()

  _visitor_functions_cache = {}

  def __init__(self):
    cls = self.__class__

    # The set of method names for each visitor implementation is assumed to
    # be fixed. Therefore this introspection can be cached.
    if cls in Visitor._visitor_functions_cache:
      enter_fns, visit_fns, leave_fns, visit_class_names = (
          Visitor._visitor_functions_cache[cls])
    else:
      enter_fns = {}
      enter_prefix = "Enter"
      enter_len = len(enter_prefix)

      visit_fns = {}
      visit_prefix = "Visit"
      visit_len = len(visit_prefix)

      leave_fns = {}
      leave_prefix = "Leave"
      leave_len = len(leave_prefix)

      for attr in dir(cls):
        if attr.startswith(enter_prefix):
          enter_fns[attr[enter_len:]] = getattr(cls, attr)
        elif attr.startswith(visit_prefix):
          visit_fns[attr[visit_len:]] = getattr(cls, attr)
        elif attr.startswith(leave_prefix):
          leave_fns[attr[leave_len:]] = getattr(cls, attr)

      ancestors = _GetAncestorMap()
      visit_class_names = set()
      # A custom Enter/Visit/Leave requires visiting all types of nodes.
      visit_all = (cls.Enter != Visitor.Enter or
                   cls.Visit != Visitor.Visit or
                   cls.Leave != Visitor.Leave)
      for node in set(enter_fns) | set(visit_fns) | set(leave_fns):
        if node in ancestors:
          visit_class_names.update(ancestors[node])
        elif node:
          # Visiting an unknown non-empty node means the visitor has defined
          # behavior on nodes that are unknown to the ancestors list.  To be
          # safe, visit everything.
          #
          # TODO(dbaum): Consider making this an error.  The only wrinkle is
          # that StrictType is unknown to _FindNodeClasses(), does not appear
          # in any preconditions, but has defined behavior in PrintVisitor.
          visit_all = True
      if visit_all:
        visit_class_names = ALL_NODE_NAMES
      Visitor._visitor_functions_cache[cls] = (
          enter_fns, visit_fns, leave_fns, visit_class_names)

    self.enter_functions = enter_fns
    self.visit_functions = visit_fns
    self.leave_functions = leave_fns
    self.visit_class_names = visit_class_names

  def Enter(self, node, *args, **kwargs):
    return self.enter_functions[node.__class__.__name__](
        self, node, *args, **kwargs)

  def Visit(self, node, *args, **kwargs):
    return self.visit_functions[node.__class__.__name__](
        self, node, *args, **kwargs)

  def Leave(self, node, *args, **kwargs):
    self.leave_functions[node.__class__.__name__](self, node, *args, **kwargs)


def InventStarArgParams(existing_names):
  """Try to find names for *args, **kwargs that aren't taken already."""
  names = {x if isinstance(x, str) else x.name
           for x in existing_names}
  args, kwargs = "args", "kwargs"
  while args in names:
    args = "_" + args
  while kwargs in names:
    kwargs = "_" + kwargs
  return (pytd.Parameter(args, pytd.NamedType("tuple"), False, True, None),
          pytd.Parameter(kwargs, pytd.NamedType("dict"), False, True, None))


class PrintVisitor(Visitor):
  """Visitor for converting ASTs back to pytd source code."""
  visits_all_node_types = True
  unchecked_node_names = ALL_NODE_NAMES

  INDENT = " " * 4
  _RESERVED = frozenset(parser_constants.RESERVED +
                        parser_constants.RESERVED_PYTHON)

  def __init__(self):
    super(PrintVisitor, self).__init__()
    self.class_names = []  # allow nested classes
    self.imports = collections.defaultdict(set)
    self.in_alias = False
    self.in_parameter = False
    self._local_names = set()
    self._class_members = set()
    self._typing_import_counts = collections.defaultdict(int)

  def _EscapedName(self, name):
    """Name, possibly escaped with backticks.

    If a name is a reserved PyTD token or contains special characters, it is
    enclosed in backticks.  See parser.Pylexer.t_NAME for legal names that
    require backticks.

    Args:
      name: A name, typically an identifier in the PyTD.

    Returns:
      The escaped name, or the original name if it doesn't need escaping.
    """
    if parser_constants.BACKTICK_NAME.search(name) or name in self._RESERVED:
      # We can do this because name will never contain backticks. Everything
      # we process here came in through the pytd parser, and the pytd syntax
      # doesn't allow escaping backticks themselves.
      return r"`" + name + "`"
    else:
      return name

  def _SafeName(self, name):
    split_name = name.split(".")
    split_result = (self._EscapedName(piece) for piece in split_name)
    return ".".join(split_result)

  def _NeedsTupleEllipsis(self, t):
    """Do we need to use Tuple[x, ...] instead of Tuple[x]?"""
    assert isinstance(t, pytd.GenericType)
    if isinstance(t, pytd.TupleType):
      return False  # TupleType is always heterogeneous.
    return t.base_type == "tuple"

  def _RequireImport(self, module, name=None):
    """Register that we're using name from module.

    Args:
      module: string identifier.
      name: if None, means we want 'import module'. Otherwise string identifier
       that we want to import.
    """
    self.imports[module].add(name)

  def _RequireTypingImport(self, name=None):
    """Convenience function, wrapper for _RequireImport("typing", name)."""
    self._RequireImport("typing", name)

  def _GenerateImportStrings(self):
    """Generate import statements needed by the nodes we've visited so far.

    Returns:
      List of strings.
    """
    ret = []
    for module in sorted(self.imports):
      names = set(self.imports[module])
      if module == "typing":
        for (name, count) in self._typing_import_counts.items():
          if not count:
            names.discard(name)
      if None in names:
        ret.append("import %s" % module)
        names.remove(None)

      if names:
        name_str = ", ".join(sorted(names))
        ret.append("from %s import %s" % (module, name_str))

    return ret

  def _IsBuiltin(self, module):
    return module == "__builtin__"

  def _FormatTypeParams(self, type_params):
    formatted_type_params = []
    for t in type_params:
      args = ["'%s'" % t.name]
      args += [c.Visit(PrintVisitor()) for c in t.constraints]
      if t.bound:
        args.append("bound=" + t.bound.Visit(PrintVisitor()))
      formatted_type_params.append(
          "%s = TypeVar(%s)" % (t.name, ", ".join(args)))
    return sorted(formatted_type_params)

  def _NameCollision(self, name):
    return name in self._class_members or name in self._local_names

  def _FromTyping(self, name):
    self._typing_import_counts[name] += 1
    if self._NameCollision(name):
      self._RequireTypingImport(None)
      return "typing." + name
    else:
      self._RequireTypingImport(name)
      return name

  def EnterTypeDeclUnit(self, unit):
    definitions = (unit.classes + unit.functions + unit.constants +
                   unit.type_params + unit.aliases)
    self._local_names = {c.name for c in definitions}

  def LeaveTypeDeclUnit(self, _):
    self._local_names = set()

  def VisitTypeDeclUnit(self, node):
    """Convert the AST for an entire module back to a string."""
    if node.type_params:
      self._FromTyping("TypeVar")
    sections = [self._GenerateImportStrings(), node.aliases, node.constants,
                self._FormatTypeParams(self.old_node.type_params), node.classes,
                node.functions]

    sections_as_string = ("\n".join(section_suite)
                          for section_suite in sections
                          if section_suite)
    return "\n\n".join(sections_as_string)

  def VisitConstant(self, node):
    """Convert a class-level or module-level constant to a string."""
    return self._SafeName(node.name) + " = ...  # type: " + node.type

  def EnterAlias(self, _):
    self.old_imports = self.imports.copy()

  def VisitAlias(self, node):
    """Convert an import or alias to a string."""
    if isinstance(self.old_node.type, pytd.NamedType):
      full_name = self.old_node.type.name
      suffix = ""
      module, _, name = full_name.rpartition(".")
      if module:
        if name != self.old_node.name:
          suffix += " as " + self.old_node.name
        self.imports = self.old_imports  # undo unnecessary imports change
        return "from " + module + " import " + name + suffix
    return self._SafeName(node.name) + " = " + node.type

  def EnterClass(self, node):
    """Entering a class - record class name for children's use."""
    n = self._SafeName(node.name)
    if node.template:
      n += "[{}]".format(
          ", ".join(t.Visit(PrintVisitor()) for t in node.template))
    for member in node.methods + node.constants:
      self._class_members.add(member.name)
    self.class_names.append(n)

  def LeaveClass(self, unused_node):
    self._class_members.clear()
    self.class_names.pop()

  def VisitClass(self, node):
    """Visit a class, producing a multi-line, properly indented string."""
    parents = node.parents
    # If classobj is the only parent, then this is an old-style class, don't
    # list any parents.
    if parents == ("classobj",):
      parents = ()
    if node.metaclass is not None:
      parents += ("metaclass=" + node.metaclass,)
    parents_str = "(" + ", ".join(parents) + ")" if parents else ""
    header = "class " + self._SafeName(node.name) + parents_str + ":"
    if node.methods or node.constants:
      # We have multiple methods, and every method has multiple signatures
      # (i.e., the method string will have multiple lines). Combine this into
      # an array that contains all the lines, then indent the result.
      constants = [self.INDENT + m for m in node.constants]
      method_lines = sum((m.splitlines() for m in node.methods), [])
      methods = [self.INDENT + m for m in method_lines]
    else:
      constants = []
      methods = [self.INDENT + "pass"]
    return "\n".join([header] + constants + methods) + "\n"

  def VisitFunction(self, node):
    """Visit function, producing multi-line string (one for each signature)."""
    function_name = self._EscapedName(node.name)
    decorators = ""
    if node.kind == pytd.STATICMETHOD and function_name != "__new__":
      decorators += "@staticmethod\n"
    elif node.kind == pytd.CLASSMETHOD:
      decorators += "@classmethod\n"
    signatures = "\n".join(decorators + "def " + function_name + sig
                           for sig in node.signatures)
    return signatures

  def VisitExternalFunction(self, node):
    """Visit function defined with PYTHONCODE."""
    return "def " + self._SafeName(node.name) + " PYTHONCODE"

  def _FormatContainerContents(self, node):
    """Print out the last type parameter of a container. Used for *args/**kw."""
    assert isinstance(node, pytd.Parameter)
    if isinstance(node.type, pytd.GenericType):
      container_name = node.type.base_type.name.rpartition(".")[2]
      assert container_name in ("tuple", "dict")
      self._typing_import_counts[container_name.capitalize()] -= 1
      return node.Replace(type=node.type.parameters[-1], optional=False).Visit(
          PrintVisitor())
    else:
      return node.Replace(type=pytd.NamedType("object"), optional=False).Visit(
          PrintVisitor())

  def VisitSignature(self, node):
    """Visit a signature, producing a string."""
    ret = " -> " + node.return_type

    # Put parameters in the right order:
    # (arg1, arg2, *args, kwonly1, kwonly2, **kwargs)
    if self.old_node.starargs is not None:
      starargs = self._FormatContainerContents(self.old_node.starargs)
    else:
      # We don't have explicit *args, but we might need to print "*", for
      # kwonly params.
      starargs = ""
    params = node.params
    for i, p in enumerate(params):
      if self.old_node.params[i].kwonly:
        assert all(p.kwonly for p in self.old_node.params[i:])
        params = params[0:i] + ("*"+starargs,) + params[i:]
        break
    else:
      if starargs:
        params += ("*" + starargs,)
    if self.old_node.starstarargs is not None:
      starstarargs = self._FormatContainerContents(self.old_node.starstarargs)
      params += ("**" + starstarargs,)

    body = []
    # Handle Mutable parameters
    # pylint: disable=no-member
    # (old_node is set in parse/node.py)
    mutable_params = [(p.name, p.mutated_type) for p in self.old_node.params
                      if p.mutated_type is not None]
    # pylint: enable=no-member
    for name, new_type in mutable_params:
      body.append("\n{indent}{name} := {new_type}".format(
          indent=self.INDENT, name=name,
          new_type=new_type.Visit(PrintVisitor())))
    for exc in node.exceptions:
      body.append("\n{indent}raise {exc}()".format(indent=self.INDENT, exc=exc))
    if not body:
      body.append(" ...")

    return "({params}){ret}:{body}".format(
        params=", ".join(params), ret=ret, body="".join(body))

  def EnterParameter(self, unused_node):
    assert not self.in_parameter
    self.in_parameter = True

  def LeaveParameter(self, unused_node):
    assert self.in_parameter
    self.in_parameter = False

  def VisitParameter(self, node):
    """Convert a function parameter to a string."""
    suffix = " = ..." if node.optional else ""
    if node.type == "object" or node.type == "Any":
      # Abbreviated form. "object" or "Any" is the default.
      if node.type == "Any":
        self._typing_import_counts["Any"] -= 1
      return node.name + suffix
    elif node.name == "self" and self.class_names and (
        node.type == self.class_names[-1]):
      return self._SafeName(node.name) + suffix
    elif node.name == "cls" and self.class_names and (
        node.type == "Type[%s]" % self.class_names[-1]):
      self._typing_import_counts["Type"] -= 1
      return self._SafeName(node.name) + suffix
    elif node.type is None:
      logging.warning("node.type is None")
      return self._SafeName(node.name)
    else:
      return self._SafeName(node.name) + ": " + node.type + suffix

  def VisitTemplateItem(self, node):
    """Convert a template to a string."""
    return node.type_param

  def VisitNamedType(self, node):
    """Convert a type to a string."""
    module, _, suffix = node.name.rpartition(".")
    if self._IsBuiltin(module) and not self._NameCollision(suffix):
      node_name = suffix
    elif module == "typing":
      node_name = self._FromTyping(suffix)
    elif module:
      self._RequireImport(module)
      node_name = node.name
    else:
      node_name = node.name
    if node_name == "NoneType":
      # PEP 484 allows this special abbreviation.
      return "None"
    else:
      return self._SafeName(node_name)

  def VisitClassType(self, node):
    return self.VisitNamedType(node)

  def VisitStrictType(self, node):
    # 'StrictType' is defined, and internally used, by booleq.py. We allow it
    # here so that booleq.py can use pytd.Print().
    return self.VisitNamedType(node)

  def VisitFunctionType(self, unused_node):
    """Convert a function type to a string."""
    return self._FromTyping("Callable")

  def VisitAnythingType(self, unused_node):
    """Convert an anything type to a string."""
    return self._FromTyping("Any")

  def VisitNothingType(self, unused_node):
    """Convert the nothing type to a string."""
    return "nothing"

  def VisitTypeParameter(self, node):
    return self._SafeName(node.name)

  def MaybeCapitalize(self, name):
    """Capitalize a generic type, if necessary."""
    # Import here due to circular import.
    from pytype.pytd import pep484  # pylint: disable=g-import-not-at-top
    capitalized = pep484.PEP484_MaybeCapitalize(name)
    if capitalized:
      return self._FromTyping(capitalized)
    else:
      return name

  def VisitGenericType(self, node):
    """Convert a generic type to a string."""
    ellipsis = ", ..." if self._NeedsTupleEllipsis(node) else ""
    param_str = ", ".join(node.parameters)
    return (self.MaybeCapitalize(node.base_type) +
            "[" + param_str + ellipsis + "]")

  def VisitCallableType(self, node):
    return "%s[[%s], %s]" % (self.MaybeCapitalize(node.base_type),
                             ", ".join(node.args), node.ret)

  def VisitTupleType(self, node):
    return self.VisitGenericType(node)

  def VisitUnionType(self, node):
    """Convert a union type ("x or y") to a string."""
    type_list = collections.OrderedDict.fromkeys(node.type_list)
    if self.in_parameter:
      # Parameter's union types are merged after as a follow up to the
      # ExpandCompatibleBuiltins visitor.
      # Import here due to circular import.
      from pytype.pytd import pep484  # pylint: disable=g-import-not-at-top
      for compat, name in pep484.COMPAT_ITEMS:
        # name can replace compat.
        if compat in type_list and name in type_list:
          del type_list[compat]
    return self._BuildUnion(type_list)

  def _BuildUnion(self, type_list):
    """Builds a union of the types in type_list.

    Args:
      type_list: A list of strings representing types.

    Returns:
      A string representing the union of the types in type_list. Simplifies
      Union[X] to X and Union[X, None] to Optional[X].
    """
    type_list = tuple(type_list)
    if len(type_list) == 1:
      return type_list[0]
    elif "None" in type_list:
      return (self._FromTyping("Optional") + "[" +
              self._BuildUnion(t for t in type_list if t != "None") + "]")
    else:
      return self._FromTyping("Union") + "[" + ", ".join(type_list) + "]"


class StripSelf(Visitor):
  """Transforms the tree into one where methods don't have the "self" parameter.

  This is useful for certain kinds of postprocessing and testing.
  """

  def VisitClass(self, node):
    """Visits a Class, and removes "self" from all its methods."""
    return node.Replace(methods=tuple(self._StripFunction(m)
                                      for m in node.methods))

  def _StripFunction(self, node):
    """Remove "self" from all signatures of a method."""
    return node.Replace(signatures=tuple(self.StripSignature(s)
                                         for s in node.signatures))

  def StripSignature(self, node):
    """Remove "self" from a Signature. Assumes "self" is the first argument."""
    return node.Replace(params=node.params[1:])


class FillInModuleClasses(Visitor):
  """Fill in ClassType pointers using symbol tables.

  This is an in-place visitor! It modifies the original tree. This is
  necessary because we introduce loops.
  """

  def __init__(self, lookup_map, fallback=None):
    """Create this visitor.

    You're expected to then pass this instance to node.Visit().

    Args:
      lookup_map: A map from names to symbol tables (i.e., objects that have a
        "Lookup" function).
      fallback: A symbol table to be tried if lookup otherwise fails.
    """
    super(FillInModuleClasses, self).__init__()
    if fallback is not None:
      lookup_map["*"] = fallback
    self._lookup_map = lookup_map

  def EnterClassType(self, node):
    """Fills in a class type.

    Args:
      node: A ClassType. This node will have a name, which we use for lookup.

    Returns:
      The same ClassType. We will have done our best to fill in its "cls"
      attribute. Call VerifyLookup() on your tree if you want to be sure that
      all of the cls pointers have been filled in.
    """
    module, _, _ = node.name.rpartition(".")
    if module:
      modules_to_try = [("", module)]
    else:
      modules_to_try = [("", ""),
                        ("", "__builtin__"),
                        ("__builtin__.", "__builtin__")]
    modules_to_try += [("", "*"), ("__builtin__.", "*")]
    for prefix, module in modules_to_try:
      mod_ast = self._lookup_map.get(module)
      if mod_ast:
        try:
          cls = mod_ast.Lookup(prefix + node.name)
        except KeyError:
          pass
        else:
          if isinstance(cls, pytd.Class):
            node.cls = cls
            return
          else:
            logging.warning("Couldn't resolve %s: Not a class: %s",
                            prefix + node.name, type(cls))


def _ToType(item, allow_constants=True):
  """Convert a pytd AST item into a type."""
  if isinstance(item, pytd.TYPE):
    return item
  elif isinstance(item, pytd.Class):
    return pytd.ClassType(item.name, item)
  elif isinstance(item, pytd.Function):
    return pytd.FunctionType(item.name, item)
  elif isinstance(item, pytd.Constant):
    if allow_constants:
      # TODO(kramm): This is wrong. It would be better if we resolve pytd.Alias
      # in the same way we resolve pytd.NamedType.
      return item
    else:
      # TODO(kramm): We should be more picky here. In particular, we shouldn't
      # allow pyi like this:
      #  object = ...  # type: int
      #  def f(x: object) -> Any
      return pytd.AnythingType()
  elif isinstance(item, pytd.Alias):
    return item.type
  else:
    raise


class DefaceUnresolved(Visitor):
  """Replace all types not in a symbol table with AnythingType."""

  unchecked_node_names = ("GenericType",)

  def __init__(self, lookup_list, do_not_log_prefix=None):
    """Create this visitor.

    Args:
      lookup_list: An iterable of symbol tables (i.e., objects that have a
        "lookup" function)
      do_not_log_prefix: If given, don't log error messages for classes with
        this prefix.
    """
    super(DefaceUnresolved, self).__init__()
    self._lookup_list = lookup_list
    self._do_not_log_prefix = do_not_log_prefix

  def VisitNamedType(self, node):
    name = node.name
    for lookup in self._lookup_list:
      try:
        cls = lookup.Lookup(name)
        if isinstance(cls, pytd.Class):
          return node
      except KeyError:
        pass
    if "." in node.name:
      return node
    else:
      if (self._do_not_log_prefix is None or
          not name.startswith(self._do_not_log_prefix)):
        logging.warning("Setting %s to ?", name)
      return pytd.AnythingType()

  def VisitCallableType(self, node):
    return self.VisitGenericType(node)

  def VisitTupleType(self, node):
    return self.VisitGenericType(node)

  def VisitGenericType(self, node):
    if isinstance(node.base_type, pytd.AnythingType):
      return node.base_type
    else:
      return node

  def VisitClassType(self, node):
    return self.VisitNamedType(node)


class NamedTypeToClassType(Visitor):
  """Change all NamedType objects to ClassType objects.
  """

  def VisitNamedType(self, node):
    """Converts a named type to a class type, to be filled in later.

    Args:
      node: The NamedType. This type only has a name.

    Returns:
      A ClassType. This ClassType will (temporarily) only have a name.
    """
    return pytd.ClassType(node.name)


class ClassTypeToNamedType(Visitor):
  """Change all ClassType objects to NameType objects.
  """

  def VisitClassType(self, node):
    return pytd.NamedType(node.name)


class DropBuiltinPrefix(Visitor):
  """Drop '__builtin__.' prefix."""

  def VisitClassType(self, node):
    _, _, name = node.name.rpartition("__builtin__.")
    return pytd.NamedType(name)

  def VisitNamedType(self, node):
    return self.VisitClassType(node)


def LookupClasses(target, global_module=None):
  """Converts a PyTD object from one using NamedType to ClassType.

  Args:
    target: The PyTD object to process. If this is a TypeDeclUnit it will also
      be used for lookups.
    global_module: Global symbols. Required if target is not a TypeDeclUnit.

  Returns:
    A new PyTD object that only uses ClassType. All ClassType instances will
    point to concrete classes.

  Raises:
    ValueError: If we can't find a class.
  """
  target = target.Visit(NamedTypeToClassType())
  module_map = {}
  if global_module is None:
    assert isinstance(target, pytd.TypeDeclUnit)
    global_module = target
  elif isinstance(target, pytd.TypeDeclUnit):
    module_map[""] = target
  target.Visit(FillInModuleClasses(module_map, fallback=global_module))
  target.Visit(VerifyLookup())
  return target


class VerifyLookup(Visitor):
  """Utility class for testing visitors.LookupClasses."""

  def EnterNamedType(self, node):
    raise ValueError("Unreplaced NamedType: %r" % node.name)

  def EnterClassType(self, node):
    if node.cls is None:
      raise ValueError("Unresolved class: %r" % node.name)


class LookupBuiltins(Visitor):
  """Look up built-in NamedTypes and give them fully-qualified names."""

  def __init__(self, builtins, full_names=True):
    """Create this visitor.

    Args:
      builtins: The builtins module.
      full_names: Whether to use fully qualified names for lookup.
    """
    super(LookupBuiltins, self).__init__()
    self._builtins = builtins
    self._full_names = full_names

  def EnterTypeDeclUnit(self, unit):
    self._current_unit = unit
    self._prefix = unit.name + "." if self._full_names else ""

  def LeaveTypeDeclUnit(self, _):
    del self._current_unit
    del self._prefix

  def VisitNamedType(self, t):
    if "." in t.name:
      return t
    try:
      self._current_unit.Lookup(self._prefix + t.name)
    except KeyError:
      # We can't find this identifier in our current module, and it isn't fully
      # qualified (doesn't contain a dot). Now check whether it's a builtin.
      try:
        item = self._builtins.Lookup(self._builtins.name + "." + t.name)
      except KeyError:
        return t
      else:
        return _ToType(item)
    else:
      return t


class LookupExternalTypes(Visitor):
  """Look up NamedType pointers using a symbol table."""

  def __init__(self, module_map, full_names=False, self_name=None):
    """Create this visitor.

    Args:
      module_map: A dictionary mapping module names to symbol tables.
      full_names: If True, then the modules in the module_map use fully
        qualified names ("collections.OrderedDict" instead of "OrderedDict")
      self_name: The name of the current module. If provided, then the visitor
        will ignore nodes with this module name.
    """
    super(LookupExternalTypes, self).__init__()
    self._module_map = module_map
    self.full_names = full_names
    self.name = self_name
    self._in_constant = False

  def _ResolveUsingGetattr(self, module_name, module):
    """Try to resolve an identifier using the top level __getattr__ function."""
    try:
      if self.full_names:
        g = module.Lookup(module_name + ".__getattr__")
      else:
        g = module.Lookup("__getattr__")
    except KeyError:
      return None
    # TODO(kramm): Make parser.py actually enforce this:
    assert len(g.signatures) == 1
    return g.signatures[0].return_type

  def EnterConstant(self, _):
    assert not self._in_constant
    self._in_constant = True

  def LeaveConstant(self, _):
    assert self._in_constant
    self._in_constant = False

  def VisitNamedType(self, t):
    """Try to look up a NamedType.

    Args:
      t: An instance of pytd.NamedType
    Returns:
      The same node t.
    Raises:
      KeyError: If we can't find a module, or an identifier in a module, or
        if an identifier in a module isn't a class.
    """
    # Drop module aliases without trying to resolve them.
    if t.name in self._module_map:
      logging.warning("Mapping module %s to Any", t.name)
      return pytd.AnythingType()
    module_name, dot, name = t.name.rpartition(".")
    if not dot or module_name == self.name:
      # Nothing to do here. This visitor will only look up nodes in other
      # modules.
      return t
    try:
      module = self._module_map[module_name]
    except KeyError:
      raise KeyError("Unknown module %s" % module_name)
    try:
      if self.full_names:
        item = module.Lookup(module_name + "." + name)
      else:
        item = module.Lookup(name)
    except KeyError:
      item = self._ResolveUsingGetattr(module_name, module)
      if item is None:
        raise KeyError("No %s in module %s" % (name, module_name))
    return _ToType(item, allow_constants=not self._in_constant)

  def VisitClassType(self, t):
    new_type = self.VisitNamedType(t)
    if isinstance(new_type, pytd.ClassType):
      t.cls = new_type.cls
      return t
    else:
      return new_type


class LookupLocalTypes(Visitor):
  """Look up local identifiers. Must be called on a TypeDeclUnit."""

  def EnterTypeDeclUnit(self, unit):
    self.unit = unit

  def LeaveTypeDeclUnit(self, _):
    del self.unit

  def VisitNamedType(self, node):
    module_name, dot, _ = node.name.rpartition(".")
    if not dot:
      try:
        item = self.unit.Lookup(self.unit.name + "." + node.name)
      except KeyError:
        # Happens for infer calling load_pytd.resolve_ast() for the final pyi
        try:
          item = self.unit.Lookup(node.name)
        except KeyError:
          raise SymbolLookupError("Couldn't find %s in %s" % (
              node.name, self.unit.name))
      return _ToType(item, allow_constants=False)
    elif module_name == self.unit.name:
      return _ToType(self.unit.Lookup(node.name), allow_constants=False)
    else:
      return node


class ReplaceTypes(Visitor):
  """Visitor for replacing types in a tree.

  This replaces both NamedType and ClassType nodes that have a name in the
  mapping. The two cases are not distinguished.
  """

  def __init__(self, mapping, record=None):
    """Initialize this visitor.

    Args:
      mapping: A dictionary, mapping strings to node instances. Any NamedType
        or ClassType with a name in this dictionary will be replaced with
        the corresponding value.
      record: Optional. A set. If given, this records which entries in
        the map were used.
    """
    super(ReplaceTypes, self).__init__()
    self.mapping = mapping
    self.record = record

  def VisitNamedType(self, node):
    if node.name in self.mapping:
      if self.record is not None:
        self.record.add(node.name)
      return self.mapping[node.name]
    return node

  def VisitClassType(self, node):
    return self.VisitNamedType(node)

  # We do *not* want to have 'def VisitClass' because that will replace a class
  # definition with itself, which is almost certainly not what is wanted,
  # because runing pytd.Print on it will result in output that's just a list of
  # class names with no contents.


class ExtractSuperClasses(Visitor):
  """Visitor for extracting all superclasses (i.e., the class hierarchy).

  When called on a TypeDeclUnit, this yields a dictionary mapping pytd.Class
  to lists of pytd.TYPE.
  """

  def __init__(self):
    super(ExtractSuperClasses, self).__init__()
    self._superclasses = {}

  def _Key(self, node):
    return node

  def VisitTypeDeclUnit(self, module):
    del module
    return self._superclasses

  def EnterClass(self, cls):
    parents = []
    for p in cls.parents:
      parent = self._Key(p)
      if parent is not None:
        parents.append(parent)
    # TODO(kramm): This uses the entire class node as a key, instead of just
    # its id.
    self._superclasses[self._Key(cls)] = parents


class ExtractSuperClassesByName(ExtractSuperClasses):
  """Visitor for extracting all superclasses (i.e., the class hierarchy).

  This returns a mapping by name, e.g. {
    "bool": ["int"],
    "int": ["object"],
    ...
  }.
  """

  def _Key(self, node):
    if isinstance(node, pytd.GenericType):
      return node.base_type.name
    elif isinstance(node, (pytd.GENERIC_BASE_TYPE, pytd.Class)):
      return node.name


class ReplaceTypeParameters(Visitor):
  """Visitor for replacing type parameters with actual types."""

  def __init__(self, mapping):
    super(ReplaceTypeParameters, self).__init__()
    self.mapping = mapping

  def VisitTypeParameter(self, p):
    return self.mapping[p]


def ClassAsType(cls):
  """Converts a pytd.Class to an instance of pytd.TYPE."""
  params = tuple(item.type_param for item in cls.template)
  if not params:
    return pytd.NamedType(cls.name)
  else:
    return pytd.GenericType(pytd.NamedType(cls.name), params)


class AdjustSelf(Visitor):
  """Visitor for setting the correct type on self.

  So
    class A:
      def f(self: object)
  becomes
    class A:
      def f(self: A)
  .
  (Notice the latter won't be printed like this, as printing simplifies the
   first argument to just "self")
  """

  def __init__(self, force=False):
    super(AdjustSelf, self).__init__()
    self.class_types = []  # allow nested classes
    self.force = force
    self.replaced_self_types = (pytd.NamedType("object"),
                                pytd.ClassType("object"),
                                pytd.ClassType("__builtin__.object"))

  def EnterClass(self, cls):
    self.class_types.append(ClassAsType(cls))

  def LeaveClass(self, unused_node):
    self.class_types.pop()

  def VisitClass(self, node):
    return node

  def VisitParameter(self, p):
    """Adjust all parameters called "self" to have their parent class type.

    But do this only if their original type is unoccupied ("object" or,
    if configured, "?").

    Args:
      p: pytd.Parameter instance.

    Returns:
      Adjusted pytd.Parameter instance.
    """
    if not self.class_types:
      # We're not within a class, so this is not a parameter of a method.
      return p
    if p.name == "self" and (self.force or p.type in self.replaced_self_types):
      return p.Replace(type=self.class_types[-1])
    else:
      return p


class RemoveUnknownClasses(Visitor):
  """Visitor for converting ClassTypes called ~unknown* to just AnythingType.

  For example, this will change
    def f(x: ~unknown1) -> ~unknown2
    class ~unknown1:
      ...
    class ~unknown2:
      ...
  to
    def f(x) -> ?
  """

  def __init__(self):
    super(RemoveUnknownClasses, self).__init__()
    self.parameter = None

  def EnterParameter(self, p):
    self.parameter = p

  def LeaveParameter(self, p):
    assert self.parameter is p
    self.parameter = None

  def VisitClassType(self, t):
    if t.name.startswith("~unknown"):
      if self.parameter:
        return pytd.NamedType("__builtin__.object")
      else:
        return pytd.AnythingType()
    else:
      return t

  def VisitNamedType(self, t):
    if t.name.startswith("~unknown"):
      if self.parameter:
        return pytd.NamedType("__builtin__.object")
      else:
        return pytd.AnythingType()
    else:
      return t

  def VisitTypeDeclUnit(self, u):
    return u.Replace(classes=tuple(
        cls for cls in u.classes if not cls.name.startswith("~unknown")))


class _CountUnknowns(Visitor):
  """Visitor for counting how often given unknowns occur in a type."""

  def __init__(self):
    super(_CountUnknowns, self).__init__()
    self.counter = collections.Counter()
    self.position = {}

  def EnterNamedType(self, t):
    _, is_unknown, suffix = t.name.partition("~unknown")
    if is_unknown:
      if suffix not in self.counter:
        # Also record the order in which we see the ~unknowns
        self.position[suffix] = len(self.position)
      self.counter[suffix] += 1

  def EnterClassType(self, t):
    return self.EnterNamedType(t)


class CreateTypeParametersForSignatures(Visitor):
  """Visitor for inserting type parameters into signatures.

  This visitor replaces re-occurring ~unknowns and the class type in __new__
  with type parameters.

  For example, this will change
    class ~unknown1:
      ...
    def f(x: ~unknown1) -> ~unknown1
  to
    _T1 = TypeVar("_T1")
    def f(x: _T1) -> _T1
  and
    class Foo:
      def __new__(cls: Type[Foo]) -> Foo
  to
    _TFoo = TypeVar("_TFoo", bound=Foo)
    class Foo:
      def __new__(cls: Type[_TFoo]) -> _TFoo
  """

  PREFIX = "_T"  # Prefix for new type params

  def __init__(self):
    super(CreateTypeParametersForSignatures, self).__init__()
    self.parameter = None
    self.class_name = None
    self.function_name = None

  def _IsIncomplete(self, name):
    return name and name.startswith("~")

  def EnterClass(self, node):
    self.class_name = node.name

  def LeaveClass(self, _):
    self.class_name = None

  def EnterFunction(self, node):
    self.function_name = node.name

  def LeaveFunction(self, _):
    self.function_name = None

  def _IsSimpleNew(self, sig):
    """Whether the signature matches a simple __new__ method.

    We're interested in whether X.__new__ has the signature
    (cls: Type[X][, ...]) -> X, since if it does, it most likely calls
    object.__new__ (via super()), so X in the signature should be replaced
    with a bounded TypeVar. (There are weird corner cases like

    class X(object):
      def __new__(cls):
        self = Y()
        self.__class__ = X
        return self

    where the replacement is wrong, but code that does things like this
    arguably shouldn't expect type-checking to work anyway.)

    Args:
      sig: A pytd.Signature.

    Returns:
      True if the signature matches a simple __new__ method, False otherwise.
    """
    if self.class_name and self.function_name and sig.params:
      # Printing the class name escapes illegal characters.
      safe_class_name = pytd.Print(pytd.NamedType(self.class_name))
      return (pytd.Print(sig.return_type) == safe_class_name and
              pytd.Print(sig.params[0].type) == "Type[%s]" % safe_class_name)
    return False

  def VisitSignature(self, sig):
    """Potentially replace ~unknowns with type parameters, in a signature."""
    if (self._IsIncomplete(self.class_name) or
        self._IsIncomplete(self.function_name)):
      # Leave unknown classes and call traces as-is, they'll never be part of
      # the output.
      # TODO(kramm): We shouldn't run on call traces in the first place.
      return sig
    counter = _CountUnknowns()
    sig.Visit(counter)
    replacements = {}
    for suffix, count in counter.counter.items():
      if count > 1:
        # We don't care whether it actually occurs in different parameters. That
        # way, e.g. "def f(Dict[T, T])" works, too.
        type_param = pytd.TypeParameter(
            self.PREFIX + str(counter.position[suffix]))
        replacements["~unknown"+suffix] = type_param
    if self._IsSimpleNew(sig):
      type_param = pytd.TypeParameter(
          self.PREFIX + self.class_name, bound=pytd.NamedType(self.class_name))
      replacements[self.class_name] = type_param
    if replacements:
      self.added_new_type_params = True
      sig = sig.Visit(ReplaceTypes(replacements))
    return sig

  def EnterTypeDeclUnit(self, _):
    self.added_new_type_params = False

  def VisitTypeDeclUnit(self, unit):
    if self.added_new_type_params:
      return unit.Visit(AdjustTypeParameters())
    else:
      return unit


# TODO(kramm): The `~unknown` functionality is becoming more important. Should
#              we have support for this on the pytd level? (That would mean
#              changing Class.name to a TYPE). Also, should we just use ~X
#              instead of ~unknownX?
class RaiseIfContainsUnknown(Visitor):
  """Find any 'unknown' Class or ClassType (not: pytd.AnythingType!) in a class.

  It throws HasUnknown on the first occurence.
  """

  class HasUnknown(Exception):
    """Used for aborting the RaiseIfContainsUnknown visitor early."""
    pass

  # COV_NF_START
  def EnterNamedType(self, _):
    raise AssertionError("This visitor needs the AST to be resolved.")
  # COV_NF_END

  def EnterClassType(self, t):
    if t.name.startswith("~unknown"):
      raise RaiseIfContainsUnknown.HasUnknown()

  def EnterClass(self, cls):
    if cls.name.startswith("~unknown"):
      raise RaiseIfContainsUnknown.HasUnknown()


class VerifyVisitor(Visitor):
  """Visitor for verifying pytd ASTs. For tests."""

  def __init__(self):
    super(VerifyVisitor, self).__init__()
    self._valid_param_name = re.compile(r"[a-zA-Z_]\w*$")

  def Enter(self, node):
    super(VerifyVisitor, self).Enter(node)
    node.Validate()

  def _AssertNoDuplicates(self, node, attrs):
    attr_to_set = {attr: {entry.name for entry in getattr(node, attr)}
                   for attr in attrs}
    # Do a quick sanity check first, and a deeper check if that fails.
    total1 = len(set.union(*attr_to_set.values()))  # all distinct names
    total2 = sum(map(len, attr_to_set.values()), 0)  # all names
    if total1 != total2:
      for a1, a2 in itertools.combinations(attrs, 2):
        both = attr_to_set[a1] & attr_to_set[a2]
        if both:
          raise AssertionError("Duplicate name(s) %s in both %s and %s" % (
              list(both), a1, a2))

  def EnterTypeDeclUnit(self, node):
    self._AssertNoDuplicates(node, ["constants", "type_params", "classes",
                                    "functions", "aliases"])
    self._all_templates = set()

  def LeaveTypeDeclUnit(self, node):
    declared_type_params = {n.name for n in node.type_params}
    for t in self._all_templates:
      if t.name not in declared_type_params:
        raise AssertionError("Type parameter %r used, but not declared. "
                             "Did you call AdjustTypeParameters?" % t.name)

  def EnterClass(self, node):
    self._AssertNoDuplicates(node, ["methods", "constants"])

  def EnterFunction(self, node):
    assert node.signatures, node

  def EnterExternalFunction(self, node):
    assert node.signatures == (), node  # pylint: disable=g-explicit-bool-comparison

  def EnterSignature(self, node):
    assert isinstance(node.has_optional, bool), node

  def EnterTemplateItem(self, node):
    self._all_templates.add(node)

  def EnterParameter(self, node):
    assert self._valid_param_name.match(node.name), node.name

  def EnterCallableType(self, node):
    self.EnterGenericType(node)

  def EnterTupleType(self, node):
    self.EnterGenericType(node)

  def EnterGenericType(self, node):
    assert node.parameters, node


class CanonicalOrderingVisitor(Visitor):
  """Visitor for converting ASTs back to canonical (sorted) ordering.
  """

  def __init__(self, sort_signatures=False):
    super(CanonicalOrderingVisitor, self).__init__()
    self.sort_signatures = sort_signatures

  def VisitTypeDeclUnit(self, node):
    return pytd.TypeDeclUnit(name=node.name,
                             constants=tuple(sorted(node.constants)),
                             type_params=tuple(sorted(node.type_params)),
                             functions=tuple(sorted(node.functions)),
                             classes=tuple(sorted(node.classes)),
                             aliases=tuple(sorted(node.aliases)))

  def VisitClass(self, node):
    return pytd.Class(name=node.name,
                      metaclass=node.metaclass,
                      parents=node.parents,
                      methods=tuple(sorted(node.methods)),
                      constants=tuple(sorted(node.constants)),
                      template=node.template)

  def VisitFunction(self, node):
    # Typically, signatures should *not* be sorted because their order
    # determines lookup order. But some pytd (e.g., inference output) doesn't
    # have that property, in which case self.sort_signatures will be True.
    if self.sort_signatures:
      return node.Replace(signatures=tuple(sorted(node.signatures)))
    else:
      return node

  def VisitSignature(self, node):
    return node.Replace(exceptions=tuple(sorted(node.exceptions)))

  def VisitUnionType(self, node):
    return pytd.UnionType(tuple(sorted(node.type_list)))


class RemoveFunctionsAndClasses(Visitor):
  """Visitor for removing unwanted functions or classes."""

  def __init__(self, names):
    super(RemoveFunctionsAndClasses, self).__init__()
    self.names = names

  def VisitTypeDeclUnit(self, node):
    return node.Replace(functions=tuple(f for f in node.functions
                                        if f.name not in self.names),
                        classes=tuple(c for c in node.classes
                                      if c.name not in self.names))


class AddNamePrefix(Visitor):
  """Visitor for making names fully qualified.

  This will change
    class Foo:
      pass
    def bar(x: Foo) -> Foo
  to (e.g. using prefix "baz"):
    class baz.Foo:
      pass
    def bar(x: baz.Foo) -> baz.Foo
  .
  """

  def __init__(self):
    super(AddNamePrefix, self).__init__()
    self.cls = None
    self.prefix = None

  def EnterTypeDeclUnit(self, node):
    self.prefix = node.name + "."
    self.classes = {cls.name for cls in node.classes}

  def EnterClass(self, cls):
    self.cls = cls

  def LeaveClass(self, cls):
    assert self.cls is cls
    self.cls = None

  def VisitClassType(self, node):
    if node.cls is not None:
      raise ValueError("AddNamePrefix visitor called after resolving")
    return self.VisitNamedType(node)

  def VisitNamedType(self, node):
    if node.name in self.classes:
      return node.Replace(name=self.prefix + node.name)
    else:
      return node

  def VisitClass(self, node):
    return node.Replace(name=self.prefix + node.name)

  def VisitTypeParameter(self, node):
    if node.scope is not None:
      return node.Replace(scope=self.prefix + node.scope)
    # Give the type parameter the name of the module it is in as its scope.
    # Module-level type parameters will keep this scope, but others will get a
    # more specific one in AdjustTypeParameters. The last character in the
    # prefix is the dot appended by EnterTypeDeclUnit, so omit that.
    return node.Replace(scope=self.prefix[:-1])

  def _VisitNamedNode(self, node):
    if self.cls:
      # class attribute
      return node
    else:
      # global constant
      return node.Replace(name=self.prefix + node.name)

  def VisitFunction(self, node):
    return self._VisitNamedNode(node)

  def VisitExternalFunction(self, node):
    return self._VisitNamedNode(node)

  def VisitConstant(self, node):
    return self._VisitNamedNode(node)

  def VisitAlias(self, node):
    return self._VisitNamedNode(node)


class CollectDependencies(Visitor):
  """Visitor for retrieving module names from external types."""

  def __init__(self):
    super(CollectDependencies, self).__init__()
    self.modules = set()

  def EnterNamedType(self, t):
    module_name, dot, unused_name = t.name.rpartition(".")
    if dot:
      self.modules.add(module_name)

  def EnterClassType(self, t):
    self.EnterNamedType(t)


def ExpandSignature(sig):
  """Expand a single signature.

  For argument lists that contain disjunctions, generates all combinations
  of arguments. The expansion will be done right to left.
  E.g., from (a or b, c or d), this will generate the signatures
  (a, c), (a, d), (b, c), (b, d). (In that order)

  Arguments:
    sig: A pytd.Signature instance.

  Returns:
    A list. The visit function of the parent of this node (VisitFunction) will
    process this list further.
  """
  params = []
  for param in sig.params:
    if isinstance(param.type, pytd.UnionType):
      # multiple types
      params.append([param.Replace(type=t) for t in param.type.type_list])
    else:
      # single type
      params.append([param])

  new_signatures = [sig.Replace(params=tuple(combination))
                    for combination in itertools.product(*params)]

  return new_signatures  # Hand list over to VisitFunction


class ExpandSignatures(Visitor):
  """Expand to Cartesian product of parameter types.

  For example, this transforms
    def f(x: int or float, y: int or float) -> str or unicode
  to
    def f(x: int, y: int) -> str or unicode
    def f(x: int, y: float) -> str or unicode
    def f(x: float, y: int) -> str or unicode
    def f(x: float, y: float) -> str or unicode

  The expansion by this class is typically *not* an optimization. But it can be
  the precursor for optimizations that need the expanded signatures, and it can
  simplify code generation, e.g. when generating type declarations for a type
  inferencer.
  """

  def VisitFunction(self, f):
    """Rebuild the function with the new signatures.

    This is called after its children (i.e. when VisitSignature has already
    converted each signature into a list) and rebuilds the function using the
    new signatures.

    Arguments:
      f: A pytd.Function instance.

    Returns:
      Function with the new signatures.
    """

    # concatenate return value(s) from VisitSignature
    signatures = sum([ExpandSignature(s) for s in f.signatures], [])
    return f.Replace(signatures=tuple(signatures))


def MergeSequences(seqs):
  """Merge a sequence of sequences into a single sequence.

  This code is copied from https://www.python.org/download/releases/2.3/mro/
  with print statements removed and modified to take a sequence of sequences.
  We use it to merge both MROs and class templates.

  Args:
    seqs: A sequence of sequences.

  Returns:
    A single sequence in which every element of the input sequences appears
    exactly once and local precedence order is preserved.

  Raises:
    ValueError: If the merge is impossible.
  """
  res = []
  while True:
    if not any(seqs):  # any empty subsequence left?
      return res
    for seq in seqs:  # find merge candidates among seq heads
      if not seq:
        continue
      cand = seq[0]
      if getattr(cand, "SINGLETON", False):
        # Special class. Cycles are allowed. Emit and remove duplicates.
        seqs = [[s for s in seq if s != cand]
                for seq in seqs]
        break
      if any(s for s in seqs if cand in s[1:] and s is not seq):
        cand = None  # reject candidate
      else:
        # Remove and emit. The candidate can be head of more than one list.
        for seq in seqs:
          if seq and seq[0] == cand:
            del seq[0]
        break
    if cand is None:
      raise ValueError
    res.append(cand)


class AdjustTypeParameters(Visitor):
  """Visitor for adjusting type parameters.

  * Inserts class templates.
  * Inserts signature templates.
  * Adds scopes to type parameters.
  """

  def __init__(self):
    super(AdjustTypeParameters, self).__init__()
    self.bound_typeparams = set()
    self.template_typeparams = None
    self.class_template = None
    self.class_name = None
    self.function_name = None
    self.constant_name = None
    self.bound_by_class = ()
    self.all_typeparams = set()

  def _GetTemplateItems(self, param):
    """Get a list of template items from a parameter."""
    items = []
    if isinstance(param, pytd.GenericType):
      for p in param.parameters:
        items.extend(self._GetTemplateItems(p))
    elif isinstance(param, pytd.UnionType):
      for p in param.type_list:
        items.extend(self._GetTemplateItems(p))
    elif isinstance(param, pytd.TypeParameter):
      items.append(pytd.TemplateItem(param))
    return items

  def VisitTypeDeclUnit(self, node):
    type_params_to_add = set()
    declared_type_params = {n.name for n in node.type_params}
    for t in self.all_typeparams:
      if t.name not in declared_type_params:
        logging.debug("Adding definition for type parameter %r", t.name)
        type_params_to_add.add(t.Replace(scope=None))
    new_type_params = node.type_params + tuple(type_params_to_add)
    return node.Replace(type_params=new_type_params)

  def EnterClass(self, node):
    """Establish the template for the class."""
    templates = []
    for parent in node.parents:
      if isinstance(parent, pytd.GenericType):
        templates.append(sum((self._GetTemplateItems(param)
                              for param in parent.parameters), []))
    try:
      template = MergeSequences(templates)
    except ValueError:
      raise ContainerError(
          "Illegal type parameter order in class %s" % node.name)

    assert self.class_template is None
    self.class_template = template

    for t in template:
      assert isinstance(t.type_param, pytd.TypeParameter)
      if t.name in self.bound_typeparams:
        raise ContainerError(
            "Duplicate type parameter %s in class %s" % (t.name, node.name))
      self.bound_typeparams.add(t.name)

    self.class_name = node.name
    self.bound_by_class = {n.type_param.name for n in template}

  def LeaveClass(self, node):
    del node
    for t in self.class_template:
      self.bound_typeparams.remove(t.name)
    self.class_name = None
    self.bound_by_class = ()
    self.class_template = None

  def VisitClass(self, node):
    """Builds a template for the class from its GenericType parents."""
    # The template items will not have been properly scoped because they were
    # stored outside of the ast and not visited while processing the class
    # subtree.  They now need to be scoped similar to VisitTypeParameter,
    # except we happen to know they are all bound by the class.
    template = [pytd.TemplateItem(t.type_param.Replace(scope=node.name))
                for t in self.class_template]
    node = node.Replace(template=tuple(template))
    return node.Visit(AdjustSelf()).Visit(NamedTypeToClassType())

  def EnterSignature(self, unused_node):
    assert self.template_typeparams is None
    self.template_typeparams = set()

  def LeaveSignature(self, unused_node):
    self.template_typeparams = None

  def VisitSignature(self, node):
    return node.Replace(template=tuple(self.template_typeparams))

  def EnterFunction(self, node):
    self.function_name = node.name

  def LeaveFunction(self, unused_node):
    self.function_name = None

  def EnterConstant(self, node):
    self.constant_name = node.name

  def LeaveConstant(self, unused_node):
    self.constant_name = None

  def _GetFullName(self, name):
    return ".".join(n for n in [self.class_name, name] if n)

  def _GetScope(self, name):
    if name in self.bound_by_class:
      return self.class_name
    return self._GetFullName(self.function_name)

  def VisitTypeParameter(self, node):
    """Add scopes to type parameters, track unbound params."""
    if self.constant_name and (not self.class_name or
                               node.name not in self.bound_by_class):
      raise ContainerError("Unbound type parameter %s in %s" % (
          node.name, self._GetFullName(self.constant_name)))
    scope = self._GetScope(node.name)
    if scope:
      node = node.Replace(scope=scope)
    else:
      # This is a top-level type parameter (TypeDeclUnit.type_params).
      # AddNamePrefix gave it the right scope, so leave it alone.
      pass

    if (self.template_typeparams is not None and
        node.name not in self.bound_typeparams):
      self.template_typeparams.add(pytd.TemplateItem(node))
    self.all_typeparams.add(node)

    return node


class VerifyContainers(Visitor):
  """Visitor for verifying containers.

  Every container (except typing.Generic) must inherit from typing.Generic and
  have an explicitly parameterized parent that is also a container. The
  parameters on typing.Generic must all be TypeVar instances. A container must
  have at most as many parameters as specified in its template.

  Raises:
    ContainerError: If a problematic container definition is encountered.
  """

  def EnterGenericType(self, node):
    if not pytd.IsContainer(node.base_type.cls):
      raise ContainerError("Class %s is not a container" % node.base_type.name)
    elif node.base_type.name == "typing.Generic":
      for t in node.parameters:
        if not isinstance(t, pytd.TypeParameter):
          raise ContainerError("Name %s must be defined as a TypeVar" % t.name)
    elif not isinstance(node, (pytd.CallableType, pytd.TupleType)):
      max_param_count = len(node.base_type.cls.template)
      actual_param_count = len(node.parameters)
      if actual_param_count > max_param_count:
        raise ContainerError(
            "Too many parameters on %s: expected %s, got %s" % (
                node.base_type.name, max_param_count, actual_param_count))

  def EnterCallableType(self, node):
    self.EnterGenericType(node)

  def EnterTupleType(self, node):
    self.EnterGenericType(node)


class ExpandCompatibleBuiltins(Visitor):
  """Ad-hoc inheritance.

  In parameters, replaces
    ClassType('__builtin__.float')
  with
    Union[ClassType('__builtin__.float'), ClassType('__builtin__.int')]

  And similarly for unicode->(unicode, str, bytes) and bool->(bool, None).

  Used to allow a function requiring a float to accept an int without making
  int inherit from float.

  See https://www.python.org/dev/peps/pep-0484/#the-numeric-tower
  """

  def __init__(self, builtins):
    super(ExpandCompatibleBuiltins, self).__init__()
    self.in_parameter = False
    self.replacements = self._BuildReplacementMap(builtins)

  @staticmethod
  def _BuildReplacementMap(builtins):
    """Dict[str, UnionType[ClassType, ...]] map."""
    prefix = builtins.name + "."
    rmap = collections.defaultdict(list)
    # Import here due to circular import.
    from pytype.pytd import pep484  # pylint: disable=g-import-not-at-top

    # compat_list :: [(compat, name)], where name is the more generalized
    # type and compat is the less generalized one. (eg: name = float, compat =
    # int)
    compat_list = itertools.chain(
        set((v, v) for _, v in pep484.COMPAT_ITEMS), pep484.COMPAT_ITEMS)

    for compat, name in compat_list:
      prefix = builtins.name + "."
      full_name = prefix + compat
      t = builtins.Lookup(full_name)
      if isinstance(t, pytd.Class):
        # Depending on python version, bytes can be an Alias, if so don't
        # want it in our union
        rmap[prefix + name].append(pytd.ClassType(full_name, t))

    return {k: pytd.UnionType(tuple(v))
            for k, v in rmap.iteritems()}

  def EnterParameter(self, _):
    assert not self.in_parameter
    self.in_parameter = True

  def LeaveParameter(self, _):
    assert self.in_parameter
    self.in_parameter = False

  def VisitClassType(self, node):
    if self.in_parameter:
      return self.replacements.get(node.name, node)
    else:
      return node


class ClearClassPointers(Visitor):
  """Set .cls pointers to 'None'."""

  def EnterClassType(self, node):
    node.cls = None


class ReplaceWithAnyReferenceVisitor(Visitor):
  """Replace all references to modules in a list with AnythingType."""

  unchecked_node_names = ("GenericType",)

  def __init__(self, module_list):
    super(ReplaceWithAnyReferenceVisitor, self).__init__()
    self._any_modules = module_list

  def VisitNamedType(self, n):
    if any(n.name.startswith(module) for module in self._any_modules):
      return pytd.AnythingType()
    return n

  def VisitGenericType(self, n):
    if isinstance(n.base_type, pytd.AnythingType):
      return pytd.AnythingType()
    return n

  def VisitClassType(self, n):
    return self.VisitNamedType(n)
