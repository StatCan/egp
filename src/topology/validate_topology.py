import click
import fiona
import geopandas as gpd
import logging
import sys
from pathlib import Path


filepath = Path(__file__).resolve()
sys.path.insert(1, str(filepath.parents[1]))
import helpers
from validation_functions import Validator


# Set logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)

# Create logger for validation errors.
logger_validations = logging.getLogger("validations")
logger_validations.setLevel(logging.WARNING)


class EGP_Topology_Validation:
    """Defines the EGP topology validation class."""

    def __init__(self, source: str, username: str, remove: bool = False) -> None:
        """
        Initializes the EGP class.

        :param str source: abbreviation for the source province / territory.
        :param str username: name of a personalized sub-directory for data editing within egp/data/interim.
        :param bool remove: remove pre-existing output file (validations.log), default False.
        """

        self.layer = f"segment_{source}"
        self.remove = remove
        self.Validator = None
        self.src = Path(filepath.parents[2] / f"data/interim/{username.lower()}/egp_data.gpkg")
        self.validations_log = Path(self.src.parent / "validations.log")

        # Configure source path and layer name.
        if self.src.exists():
            if self.layer not in set(fiona.listlayers(self.src)):
                logger.exception(f"Layer \"{self.layer}\" not found within source: \"{self.src}\".")
                sys.exit(1)
        else:
            logger.exception(f"Source not found: \"{self.src}\".")
            sys.exit(1)

        # Configure destination path.
        if self.validations_log.exists():
            if remove:
                logger.info(f"Removing conflicting file: \"{self.validations_log}\".")
                self.validations_log.unlink()
            else:
                logger.exception(f"Conflicting file exists (\"{self.validations_log}\") but remove=False. Set "
                                 f"remove=True (-r) or manually clear the output namespace.")
                sys.exit(1)

        # Load source data.
        logger.info(f"Loading source data: {self.src}|layer={self.layer}.")
        self.segment = gpd.read_file(self.src, layer=self.layer)
        logger.info("Successfully loaded source data.")

    def log_errors(self) -> None:
        """Outputs error logs returned by validation functions."""

        logger.info(f"Writing error logs: \"{self.validations_log}\".")

        # Add File Handler to validation logger.
        f_handler = logging.FileHandler(self.validations_log)
        f_handler.setLevel(logging.WARNING)
        f_handler.setFormatter(logger.handlers[0].formatter)
        logger_validations.addHandler(f_handler)

        # Iterate and log errors.
        for code, errors in sorted(self.Validator.errors.items()):

            # Format and write logs.
            errors["values"] = "\n".join(map(str, errors["values"]))
            if errors["query"]:
                logger_validations.warning(f"{code}\n\nValues:\n{errors['values']}\n\nQuery: {errors['query']}\n")
            else:
                logger_validations.warning(f"{code}\n\nValues:\n{errors['values']}\n")

    def validations(self) -> None:
        """Applies a set of validations to segments."""

        logger.info("Initiating validator.")

        # Instantiate and execute validator class.
        self.Validator = Validator(self.segment, dst=self.src, layer=self.layer)
        self.Validator.execute()

    def execute(self) -> None:
        """Executes the EGP class."""

        self.validations()
        self.log_errors()


@click.command()
@click.argument("source", type=click.Choice("ab bc mb nb nl ns nt nu on pe qc sk yt".split(), False))
@click.argument("username", type=click.STRING)
@click.option("--remove / --no-remove", "-r", default=False, show_default=True,
              help="Remove pre-existing output file (validations.log).")
def main(source: str, username: str, remove: bool = False) -> None:
    """
    Instantiates and executes the EGP class.

    :param str source: abbreviation for the source province / territory.
    :param str username: name of a personalized sub-directory for data editing within egp/data/interim.\n
    :param bool remove: remove pre-existing output file (validations.log), default False.
    """

    try:

        with helpers.Timer():
            egp = EGP_Topology_Validation(source, username, remove)
            egp.execute()

    except KeyboardInterrupt:
        logger.exception("KeyboardInterrupt: Exiting program.")
        sys.exit(1)


if __name__ == "__main__":
    main()
