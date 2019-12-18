import calendar
import logging
import networkx as nx
import numpy as np
import os
import sys
from datetime import datetime

sys.path.insert(1, os.path.join(sys.path[0], ".."))
import helpers


logger = logging.getLogger()


def strip_whitespace(val):
    """Strips leading and trailing whitespace from the given value."""

    return val.strip()


def validate_dates(credate, revdate, default):
    """
    Applies a set of validations to credate and revdate fields.
    Parameter default is assumed to be identical for credate and revdate fields.
    """

    credate, revdate, default = map(str, [credate, revdate, default])

    # Get current date.
    today = datetime.today().strftime("%Y%m%d")

    # Validation.
    def validate(date):

        if date != default:

            # Validation: length must be 4, 6, or 8.
            if len(date) not in (4, 6, 8):
                raise ValueError("Invalid length for credate / revdate = \"{}\".".format(date))

            # Rectification: default to 01 for missing month and day values.
            while len(date) in (4, 6):
                date += "01"

            # Validation: valid values for day, month, year (1960+).
            year, month, day = map(int, [date[:4], date[4:6], date[6:8]])

            # Year.
            if not 1960 <= year <= int(today[:4]):
                raise ValueError("Invalid year for credate / revdate at index 0:3 = \"{}\".".format(year))

            # Month.
            if month not in range(1, 12 + 1):
                raise ValueError("Invalid month for credate / revdate at index 4:5 = \"{}\".".format(month))

            # Day.
            if not 1 <= day <= calendar.mdays[month]:
                if not all([day == 29, month == 2, calendar.isleap(year)]):
                    raise ValueError("Invalid day for credate / revdate at index 6:7 = \"{}\".".format(day))

            # Validation: ensure value <= today.
            if year == today[:4]:
                if not all([month <= today[4:6], day <= today[6:8]]):
                    raise ValueError("Invalid date for credate / revdate = \"{}\". "
                                     "Date cannot be in the future.".format(date, today))

        return date

    # Validation: individual date validations.
    credate = validate(credate)
    revdate = validate(revdate)

    # Validation: ensure credate <= revdate.
    if credate != default and revdate != default:
        if not int(credate) <= int(revdate):
            raise ValueError("Invalid date combination for credate = \"{}\", revdate = \"{}\". "
                             "credate must precede or equal revdate.".format(credate, revdate))

    return credate, revdate


def validate_nbrlanes(nbrlanes, default):
    """Applies a set of validations to nbrlanes field."""

    # Validation: ensure 1 <= nbrlanes <= 8.
    if str(nbrlanes) != str(default):
        if not 1 <= int(nbrlanes) <= 8:
            raise ValueError("Invalid value for nbrlanes = \"{}\". Value must be between 1 and 8.".format(nbrlanes))

    return nbrlanes


def validate_pavement(pavstatus, pavsurf, unpavsurf):
    """Applies a set of validations to pavstatus, pavsurf, and unpavsurf fields."""

    # Validation: when pavstatus == "Paved", ensure pavsurf != "None" and unpavsurf == "None".
    if pavstatus == "Paved":
        if pavsurf == "None":
            raise ValueError("Invalid combination for pavstatus = \"{}\", pavsurf = \"{}\". When pavstatus is "
                             "\"Paved\", pavsurf must not be \"None\".".format(pavstatus, pavsurf))
        if unpavsurf != "None":
            raise ValueError("Invalid combination for pavstatus = \"{}\", unpavsurf = \"{}\". When pavstatus is "
                             "\"Paved\", unpavsurf must be \"None\".".format(pavstatus, unpavsurf))

    # Validation: when pavstatus == "Unpaved", ensure pavsurf == "None" and unpavsurf != "None".
    if pavstatus == "Unpaved":
        if pavsurf != "None":
            raise ValueError("Invalid combination for pavstatus = \"{}\", pavsurf = \"{}\". When pavstatus is "
                             "\"Unpaved\", pavsurf must be \"None\".".format(pavstatus, pavsurf))
        if unpavsurf == "None":
            raise ValueError("Invalid combination for pavstatus = \"{}\", unpavsurf = \"{}\". When pavstatus is "
                             "\"Unpaved\", unpavsurf must not be \"None\".".format(pavstatus, pavsurf))

    return pavstatus, pavsurf, unpavsurf


def validate_roadclass_rtnumber1(roadclass, rtnumber1, default):
    """
    Applies a set of validations to roadclass and rtnumber1 fields.
    Parameter default should refer to rtnumber1.
    """

    # Validation: ensure rtnumber1 is not the default value when roadclass == "Freeway" or "Expressway / Highway".
    if roadclass in ("Freeway", "Expressway / Highway"):
        if str(rtnumber1) == str(default):
            raise ValueError(
                "Invalid value for rtnumber1 = \"{}\". When roadclass is \"Freeway\" or \"Expressway / Highway\", "
                "rtnumber1 must not be the default field value = \"{}\".".format(rtnumber1, default))

    return roadclass, rtnumber1


def validate_route_text(df, default):
    """
    Applies a set of validations to route attributes:
        rtename1en, rtename2en, rtename3en, rtename4en,
        rtename1fr, rtename2fr, rtename3fr, rtename4fr.
    Parameter default should be a dictionary with a key for each of the required fields.
    """

    # Validation: set text-based route fields to title case.
    cols = ["rtename1en", "rtename2en", "rtename3en", "rtename4en",
            "rtename1fr", "rtename2fr", "rtename3fr", "rtename4fr"]
    for col in cols:
        df[col] = df[col].map(lambda route: route if route == default[col] else route.title())

    return df


def validate_route_contiguity(df, default):
    """
    Applies a set of validations to route attributes (rows represent field groups):
        rtename1en, rtename2en, rtename3en, rtename4en,
        rtename1fr, rtename2fr, rtename3fr, rtename4fr,
        rtnumber1, rtnumber2, rtnumber3, rtnumber4, rtnumber5.
    Parameter default should be a dictionary with a key for each of the required fields.
    """

    # Validation: ensure route has contiguous geometry.
    for field_group in [["rtename1en", "rtename2en", "rtename3en", "rtename4en"],
                        ["rtename1fr", "rtename2fr", "rtename3fr", "rtename4fr"],
                        ["rtnumber1", "rtnumber2", "rtnumber3", "rtnumber4", "rtnumber5"]]:

        # Compile route names.
        route_names = [df[col].unique() for col in field_group]
        # Remove default values.
        route_names = [names[np.where(names != default[field_group[index]])] for index, names in enumerate(route_names)]
        # Concatenate arrays.
        route_names = np.concatenate(route_names, axis=None)

        # Iterate route names.
        for route_name in route_names:

            # Subset dataframe to those records with route name in at least one field.
            route_df = df.iloc[list(np.where(df[field_group] == route_name)[0])]

            # Load dataframe as networkx graph.
            route_graph = helpers.gdf_to_nx(route_df, keep_attributes=False)

            # Validate contiguity (networkx connectivity).
            if not nx.is_connected(route_graph):

                # Identify deadends (locations of discontiguity), limit to 20.
                deadends = [coords for coords, degree in route_graph.degree() if degree == 1]
                deadends = "\n".join(["{}, {}".format(*deadend) for deadend in deadends])

                raise ValueError("Invalid route = \"{}\", based on route attributes: {}. Route must be contiguous. "
                                 "Review contiguity at the following endpoints:\n{}"
                                 .format(route_name, ", ".join(field_group), deadends))


def validate_speed(speed, default):
    """Applies a set of validations to speed field."""

    if str(speed) != str(default):

        # Validation: ensure 5 <= speed <= 120.
        if not 5 <= int(speed) <= 120:
            raise ValueError("Invalid value for speed = \"{}\". Value must be between 5 and 120.".format(speed))

        # Validation: ensure speed is a multiple of 5.
        if int(speed) % 5 != 0:
            raise ValueError("Invalid value for speed = \"{}\". Value must be a multiple of 5.".format(speed))

    return speed
