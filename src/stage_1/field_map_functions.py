import logging
import numpy as np
import re
import sys
from copy import deepcopy
from numpy import nan
from operator import attrgetter, itemgetter


logger = logging.getLogger()


def apply_domain(val, domain):
    """Applies a domain restriction to the given value based on the provided domain dictionary or list."""

    val = str(val).lower()

    # Validate against domain dictionary.
    if isinstance(domain, dict):

        # Validate against keys.
        for key in domain:
            if val == str(key).lower():
                return domain[key][0]

        # Validate values as list.
        domain = domain.values()

    # Validate against domain list.
    for values in domain:
        if val in map(str.lower, map(str, values)):
            return values[0]

    return nan


def copy_attribute_functions(field_mapping_attributes, params):
    """
    Compiles the field mapping functions for each of the given field mapping attributes (target table columns).
    Adds / updates any parameters provided for those functions.

    Possible yaml construction of copy_attribute_functions:

    1) copy_attribute_functions:                              2) copy_attribute_functions:
         attributes: [attribute_1, attribute_2, ...]               attributes:
         modify_parameters:                                          - attribute_1:
           function:                                                     function:
             parameter: Value                                              parameter: Value
           ...:                                                          ...:
             ...: ...                                                      ...: ...
                                                                     - attribute_2
                                                                     - ...
    """

    # Validate inputs.
    validate_dtypes("copy_attribute_functions", params, dict)
    validate_dtypes("copy_attribute_functions[\"attributes\"]", params["attributes"], list)
    if "modify_parameters" in params.keys():
        for index, attribute in enumerate(params["attributes"]):
            validate_dtypes("copy_attribute_functions[\"attributes\"][{}]".format(index), attribute, str)
        for func in params["modify_parameters"]:
            validate_dtypes("copy_attribute_functions[\"modify_parameters\"][\"{}\"]".format(func),
                            params["modify_parameters"][func], dict)
    else:
        for index, attribute in enumerate(params["attributes"]):
            validate_dtypes("copy_attribute_functions[\"attributes\"][{}]".format(index), attribute, [str, dict])
            if isinstance(attribute, dict):
                for func in attribute:
                    validate_dtypes("copy_attribute_functions[\"attributes\"][\"{}\"]".format(func), attribute[func],
                                    dict)

    # Iterate attributes to compile function-parameter dictionaries.
    attribute_func_dicts = list()

    for attribute in params["attributes"]:

        # Retrieve attribute name and parameter modifications.
        mod_params = params["modify_parameters"] if "modify_parameters" in params else dict()
        if isinstance(attribute, dict):
            attribute, mod_params = list(attribute.items())[0]

        # Retrieve attribute field mapping functions.
        attribute_func_dict = deepcopy(field_mapping_attributes[attribute]["functions"])

        # Apply modified parameters.
        for attribute_func, attribute_params in mod_params.items():
            for attribute_param, attribute_param_value in attribute_params.items():
                attribute_func_dict[attribute_func][attribute_param] = attribute_param_value

        # Store result.
        attribute_func_dicts.append(attribute_func_dict)

    return attribute_func_dicts


def direct(val, **kwargs):
    """
    Returns the given value. Intended to provide a function call for direct (1:1) field mapping.

    Possible yaml construction of direct field mapping:

    1) target_field:                                2) target_field: source_field or raw value
         fields: source_field or raw value
         functions:
           direct:
             parameter: None
    """

    return nan if val in (None, "", nan) else val


def regex_find(val, pattern, match_index, group_index, domain=None, strip_result=False):
    """
    Extracts a value's nth match (index) from the nth match group (index) based on a regular expression pattern.
    Case ignored by default.
    Parameter 'group_index' can be an int or list of ints, the returned value will be at the first index with a match.
    Parameter 'strip_result' returns the entire value except for the extracted substring.
    """

    # Return numpy nan.
    if val in (None, "", nan):
        return nan

    # Validate inputs.
    pattern = validate_regex(pattern, domain)
    validate_dtypes("match_index", match_index, [int, np.int_])
    if not isinstance(group_index, list):
        group_index = [group_index]
    for index, i in enumerate(group_index):
        validate_dtypes("group_index[{}]".format(index), i, [int, np.int_])
    validate_dtypes('strip_result', strip_result, [bool, np.bool_])

    # Apply and return regex value, or numpy nan.
    try:

        # Single group index.
        if isinstance(group_index, int):
            matches = re.finditer(pattern, val, flags=re.IGNORECASE)
            result = [[m.groups()[group_index], m.start(), m.end()] for m in matches][match_index]

        # Multiple group indexes.
        else:
            matches = re.finditer(pattern, val, flags=re.IGNORECASE)
            result = [[itemgetter(*group_index)(m.groups()), m.start(), m.end()] for m in matches][match_index]
            result[0] = [grp for grp in result[0] if grp not in (None, "", nan)][0]

        # Strip result if required.
        if strip_result:
            start, end = result[1:]
            return " ".join(map(str, [val[:start], val[end:]])).strip()
        else:
            return result[0]

    except (IndexError, ValueError):
        return val if strip_result else nan


def regex_sub(val, pattern_from, pattern_to, domain=None):
    """
    Substitutes one regular expression pattern with another.
    Case ignored by default.
    """

    # Return numpy nan.
    if val in (None, "", nan):
        return nan

    # Validate inputs.
    pattern_from = validate_regex(pattern_from, domain)
    pattern_to = validate_regex(pattern_to, domain)

    # Apply and return regex value.
    return re.sub(pattern_from, pattern_to, val, flags=re.IGNORECASE)


def split_record(vals, field=None):
    """
    If vals = pandas dataframe: Splits records on the given field.
    If vals = numpy ndarray: Returns value.

    This function is executed in two separate parts due to the functionality of stage_1.apply_functions, which operates
    on a series. Since splitting records in a series would not affect the original dataframe, the input series is simply
    returned. This function is then called again within stage_1, once all mapping functions have been applied to the
    target dataframe, to execute the row splitting.

    It was decided to keep split_record as a callable field mapping function instead of creating a separate function
    within stage_1 such that split_record can exist within a chain of other field mapping functions.
    """

    # Return values.
    if isinstance(vals, np.ndarray):

        return vals

    # Split records.
    else:

        # Identify records to be split.
        vals_split = vals.loc[vals[field].map(lambda val: val[0] != val[1])].copy(deep=True)

        if len(vals_split):

            # Keep first instance for original records.
            vals[field] = vals[field].map(lambda val: val[0])

            # Keep second instance for split records.
            vals_split[field] = vals_split[field].map(lambda val: val[1])

            # Append split records to dataframe.
            vals = vals.append(vals_split, ignore_index=False)

        return vals


def validate_dtypes(val_name, val, dtypes):
    """Validates one or more data types."""

    if not isinstance(dtypes, list):
        dtypes = [dtypes]

    if any([isinstance(val, dtype) for dtype in dtypes]):
        return True
    else:
        logger.exception("Validation failed. Invalid data type for \"{}\": \"{}\". Expected {} but received {}.".format(
            val_name, val, " or ".join(map(attrgetter("__name__"), dtypes)), type(val).__name__))
        sys.exit(1)


def validate_regex(pattern, domain=None):
    """
    Validates a regular expression.
    Replaces keyword 'domain' with the domain values of the given field, if provided.
    """

    try:

        # Compile regular expression.
        re.compile(pattern)

        # Load domain values.
        if pattern.find("domain") >= 0 and domain is not None:
            pattern = pattern.replace("domain", "|".join(map(str, domain)))

        return pattern

    except re.error:
        logger.exception("Validation failed. Invalid regular expression: \"{}\".".format(pattern))
        sys.exit(1)
