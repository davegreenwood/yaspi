"""yaspi - yet another python slurm interface
"""

import os
import re
import psutil
import argparse
import subprocess
from pathlib import Path
from watchlogs.watchlogs import Watcher
from itertools import zip_longest


class Yaspi:

    def __init__(self, job_name, cmd, recipe, gen_script_dir, template_dir, log_dir,
                 partition, job_array_size, cpus_per_task, gpus_per_task, refresh_logs,
                 env_setup=None):
        self.cmd = cmd
        self.log_dir = log_dir
        self.recipe = recipe
        self.job_name = job_name
        self.partition = partition
        self.env_setup = env_setup
        self.refresh_logs = refresh_logs
        self.template_dir = template_dir
        self.cpus_per_task = cpus_per_task
        self.gpus_per_task = gpus_per_task
        self.gen_script_dir = gen_script_dir
        self.job_array_size = job_array_size
        self.slurm_logs = None
        self.generate_scripts()

    def generate_scripts(self):
        gen_dir = Path(self.gen_script_dir)
        if self.recipe == "ray":
            # TODO(Samuel): configure this more sensibly
            if self.env_setup is None:
                self.env_setup = (
                    'export PYTHONPATH="${BASE}":$PYTHONPATH\n'
                    'export PATH="${HOME}/local/anaconda3/condabin/:$PATH"\n'
                    'source ~/local/anaconda3/etc/profile.d/conda.sh\n'
                    'conda activate pt37'
                )
            template_paths = {
                "master": "ray/ray-master.sh",
                "sbatch": "ray/ray-sbatch.sh",
                "head-node": "ray/start-ray-head-node.sh",
                "worker-node": "ray/start-ray-worker-node.sh",
            }
            self.log_path = str(Path(self.log_dir) / self.job_name / "%4a-log.txt")
            rules = {
                "master": {
                    "nfs_update_secs": 1,
                    "ray_sbatch_path": str(gen_dir / template_paths["sbatch"]),
                },
                "sbatch": {
                    "cmd": self.cmd,
                    "log_path": self.log_path,
                    "job_name": self.job_name,
                    "partition": self.partition,
                    "env_setup": self.env_setup,
                    "array": f"1-{self.job_array_size}",
                    "cpus_per_task": self.cpus_per_task,
                    "approx_ray_init_time_in_secs": 10,
                    "head_init_script": str(gen_dir / template_paths["head-node"]),
                    "worker_init_script": str(gen_dir / template_paths["worker-node"]),
                },
                "head-node": {
                    "env_setup": self.env_setup,
                },
                "worker-node": {
                    "env_setup": self.env_setup,
                },
            }
            if self.gpus_per_task:
                resource_str = f"SBATCH --gres=gpu:{self.gpus_per_task}"
                rules["sbatch"]["sbatch_resources"] = resource_str
        else:
            raise ValueError(f"template: {self.recipe} unrecognised")

        template_paths = {key: Path(self.template_dir) / val
                          for key, val in template_paths.items()}

        self.gen_scripts = {}
        for key, template_path in template_paths.items():
            gen = self.fill_template(template_path=template_path, rules=rules[key])
            dest_path = gen_dir / Path(template_path).relative_to(self.template_dir)
            self.gen_scripts[key] = dest_path
            dest_path.parent.mkdir(exist_ok=True, parents=True)
            with open(str(dest_path), "w") as f:
                print(f"Writing slurm script ({key}) to {dest_path}")
                f.write(gen)
            dest_path.chmod(0o755)

    def get_log_paths(self):
        watched_logs = []
        for idx in range(self.job_array_size):
            slurm_id = idx + 1
            watched_log = Path(str(self.log_path).replace("%4a", f"{slurm_id:04d}"))
            watched_log.parent.mkdir(exist_ok=True, parents=True)
            if self.refresh_logs:
                if watched_log.exists():
                    watched_log.unlink()
            # We must make sure that the log file exists to enable monitoring
            if not watched_log.exists():
                print(f"Creating watch log: {watched_log} for the first time")
                watched_log.touch()
            watched_logs.append(str(watched_log.resolve()))
        return watched_logs

    def submit(self, watch=True):
        if watch:
            watched_logs = self.get_log_paths()
        submission_cmd = f"source {self.gen_scripts['master']}"
        print(f"Submitting job with command: {submission_cmd}")
        os.system(submission_cmd)
        if watch:
            Watcher(watched_logs=watched_logs).run()


    def fill_template(self, template_path, rules):
        """TDDO(Samuel)
        """
        generated = []
        with open(template_path, "r") as f:
            template = f.read().splitlines()
        for row in template:
            edits = []
            regex = r"\{\{(.*?)\}\}"
            for match in re.finditer(regex, row):
                groups = match.groups()
                assert len(groups) == 1, "expected single group"
                key = groups[0]
                token = rules[key]
                edits.append((match.span(), token))
            if edits:
                # invert the spans
                spans = [(None, 0)] + [x[0] for x in edits] + [(len(row), None)]
                inverse_spans = [(x[1], y[0]) for x, y in zip(spans, spans[1:])]
                tokens = [row[start:stop] for start, stop in inverse_spans]
                urls = [str(x[1]) for x in edits]
                new_row = ""
                for token, url in zip_longest(tokens, urls, fillvalue=""):
                    new_row += token + url
                row = new_row
            generated.append(row)
        return "\n".join(generated)


def main():
    parser = argparse.ArgumentParser(description="yaspi tool")
    parser.add_argument("--job_name", default="yaspi-test",
                        help="the name that slurm will give to the job")
    parser.add_argument("--recipe", default="ray",
                        help="the SLURM recipe to use to generate scripts")
    parser.add_argument("--template_dir", default="templates",
                        help="the directory containing the source templates for SLURM")
    parser.add_argument("--partition", default="gpu",
                        help="The name of the SLURM partition used to run the job")
    parser.add_argument("--gen_script_dir", default="data/slurm-gen-scripts",
                        help="directory in which generated slurm scripts will be stored")
    parser.add_argument("--cmd", default='echo "hello"',
                        help="single command (or comma separated commands) to run")
    parser.add_argument("--job_array_size", type=int, default=2,
                        help="The number of SLURM array workers")
    parser.add_argument("--cpus_per_task", type=int, default=5,
                        help="the number of cpus requested for each SLURM task")
    parser.add_argument("--gpus_per_task", type=int, default=1,
                        help="the number of gpus requested for each SLURM task")
    parser.add_argument("--env_setup", help="setup string for a custom environment")
    parser.add_argument("--log_dir", default="data/slurm-logs",
                        help="location where SLURM logs will be stored")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--refresh_logs", action="store_true")
    parser.add_argument("--watch", type=int, default=1,
                        help="whether to watch the generated SLURM logs")
    args = parser.parse_args()

    job = Yaspi(
        cmd=args.cmd,
        log_dir=args.log_dir,
        job_name=args.job_name,
        recipe=args.recipe,
        partition=args.partition,
        template_dir=args.template_dir,
        gen_script_dir=args.gen_script_dir,
        job_array_size=args.job_array_size,
        cpus_per_task=args.cpus_per_task,
        gpus_per_task=args.gpus_per_task,
        refresh_logs=args.refresh_logs,
        env_setup=args.env_setup,
    )
    job.submit(watch=True)

if __name__ == "__main__":
    main()