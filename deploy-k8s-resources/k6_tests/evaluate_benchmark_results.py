#!/usr/bin/env python3
"""
evaluate_benchmark_results.py — Production-Grade Benchmark Gate Evaluation

This module provides functions to evaluate benchmark results against SLOs,
detect statistical instability, and generate pass/fail verdicts.

Designed for multi-gateway comparison (Kong, LiteLLM, etc.)

Usage:
    # As a library
    from evaluate_benchmark_results import BenchmarkEvaluator
    evaluator = BenchmarkEvaluator('benchmark_config.yaml')
    verdict = evaluator.evaluate_campaign('summary.csv', 'token-chat-openai')

    # As a CLI
    python3 evaluate_benchmark_results.py summary.csv --scenario token-chat-openai
"""

import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class Verdict(Enum):
    """Benchmark evaluation verdict."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNSTABLE = "UNSTABLE"  # High variance, results unreliable
    INCOMPLETE = "INCOMPLETE"  # Not enough data


@dataclass
class Violation:
    """A single SLO or stability violation."""
    metric: str
    threshold: float
    actual: float
    severity: str  # "error" or "warning"
    load_value: Optional[str] = None
    message: str = ""


@dataclass
class EvaluationResult:
    """Complete evaluation result for a campaign."""
    verdict: Verdict
    scenario: str
    gateway: str
    total_runs: int
    succeeded_runs: int
    failed_runs: int
    violations: List[Violation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics_summary: Dict[str, Dict[str, float]] = field(default_factory=dict)
    stability_analysis: Dict[str, float] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "scenario": self.scenario,
            "gateway": self.gateway,
            "total_runs": self.total_runs,
            "succeeded_runs": self.succeeded_runs,
            "failed_runs": self.failed_runs,
            "violations": [
                {
                    "metric": v.metric,
                    "threshold": v.threshold,
                    "actual": v.actual,
                    "severity": v.severity,
                    "load_value": v.load_value,
                    "message": v.message,
                }
                for v in self.violations
            ],
            "warnings": self.warnings,
            "metrics_summary": self.metrics_summary,
            "stability_analysis": self.stability_analysis,
        }
    
    def to_markdown(self) -> str:
        """Generate markdown report."""
        lines = [
            f"# Benchmark Evaluation Report",
            "",
            f"**Scenario:** {self.scenario}",
            f"**Gateway:** {self.gateway}",
            f"**Verdict:** {'✅' if self.verdict == Verdict.PASSED else '❌'} **{self.verdict.value}**",
            "",
            "## Summary",
            "",
            f"- Total runs: {self.total_runs}",
            f"- Succeeded: {self.succeeded_runs}",
            f"- Failed: {self.failed_runs}",
            "",
        ]
        
        if self.violations:
            lines.extend([
                "## Violations",
                "",
                "| Metric | Threshold | Actual | Severity | Load |",
                "|--------|-----------|--------|----------|------|",
            ])
            for v in self.violations:
                lines.append(
                    f"| {v.metric} | {v.threshold:.2f} | {v.actual:.2f} | {v.severity} | {v.load_value or '-'} |"
                )
            lines.append("")
        
        if self.stability_analysis:
            lines.extend([
                "## Stability Analysis",
                "",
                "| Metric | CV (%) | Status |",
                "|--------|--------|--------|",
            ])
            for metric, cv in self.stability_analysis.items():
                status = "⚠️ High variance" if cv > 15 else "✅ Stable"
                lines.append(f"| {metric} | {cv:.2f} | {status} |")
            lines.append("")
        
        if self.warnings:
            lines.extend([
                "## Warnings",
                "",
            ])
            for w in self.warnings:
                lines.append(f"- {w}")
            lines.append("")
        
        return "\n".join(lines)


class BenchmarkEvaluator:
    """
    Evaluates benchmark results against SLOs and stability criteria.
    """
    
    def __init__(self, config_path: Optional[str] = None):
        self.config = self._load_config(config_path)
    
    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load benchmark configuration."""
        if config_path is None:
            config_path = Path(__file__).parent / "benchmark_config.yaml"
        
        config_path = Path(config_path)
        if not config_path.exists():
            # Return sensible defaults
            return {
                "global": {
                    "min_repeats": 3,
                    "max_coefficient_of_variation_percent": 15.0,
                },
                "slo": {
                    "max_error_rate_percent": 1.0,
                    "min_checks_pass_rate_percent": 99.0,
                    "max_p95_latency_ms": 500,
                    "max_p99_latency_ms": 1000,
                    "max_dropped_iterations_rate_percent": 5.0,
                },
                "scenarios": {},
                "gateways": {
                    "kong": {"name": "Kong AI Gateway"},
                },
            }
        
        with open(config_path) as f:
            return yaml.safe_load(f)
    
    def _get_scenario_thresholds(self, scenario: str) -> Dict[str, float]:
        """Get SLO thresholds for a specific scenario."""
        base = self.config.get("slo", {})
        overrides = self.config.get("scenarios", {}).get(scenario, {})
        
        thresholds = {
            "max_error_rate_percent": base.get("max_error_rate_percent", 1.0),
            "min_checks_pass_rate_percent": base.get("min_checks_pass_rate_percent", 99.0),
            "max_p95_latency_ms": base.get("max_p95_latency_ms", 500),
            "max_p99_latency_ms": base.get("max_p99_latency_ms", 1000),
            "max_dropped_iterations_rate_percent": base.get("max_dropped_iterations_rate_percent", 5.0),
        }
        
        # Apply scenario-specific overrides
        for key in ["max_p95_latency_ms", "max_p99_latency_ms", "max_ttft_p95_ms"]:
            if key in overrides:
                thresholds[key] = overrides[key]
        
        return thresholds
    
    def _calculate_cv(self, values: List[float]) -> float:
        """Calculate coefficient of variation (CV) as percentage."""
        if len(values) < 2:
            return 0.0
        mean = statistics.mean(values)
        if mean == 0:
            return 0.0
        stddev = statistics.stdev(values)
        return (stddev / mean) * 100
    
    def _parse_float(self, value: str) -> float:
        """Safely parse float from string."""
        try:
            return float(value) if value else 0.0
        except (ValueError, TypeError):
            return 0.0
    
    def evaluate_campaign(
        self,
        summary_csv_path: str,
        scenario: str,
        gateway: str = "kong",
    ) -> EvaluationResult:
        """
        Evaluate a complete campaign from its summary CSV.
        
        Args:
            summary_csv_path: Path to campaign summary.csv
            scenario: Scenario name (e.g., "token-chat-openai")
            gateway: Gateway name (e.g., "kong", "litellm")
        
        Returns:
            EvaluationResult with verdict and details
        """
        # Load data
        with open(summary_csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        
        if not rows:
            return EvaluationResult(
                verdict=Verdict.INCOMPLETE,
                scenario=scenario,
                gateway=gateway,
                total_runs=0,
                succeeded_runs=0,
                failed_runs=0,
                warnings=["No data in summary CSV"],
            )
        
        # Get thresholds
        thresholds = self._get_scenario_thresholds(scenario)
        max_cv = self.config.get("global", {}).get("max_coefficient_of_variation_percent", 15.0)
        min_repeats = self.config.get("global", {}).get("min_repeats", 3)
        
        # Group by load value
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for row in rows:
            load_key = row.get("load_value", "unknown")
            groups[load_key].append(row)
        
        violations: List[Violation] = []
        warnings: List[str] = []
        metrics_summary: Dict[str, Dict[str, float]] = {}
        stability_analysis: Dict[str, float] = {}
        
        total_runs = len(rows)
        succeeded_runs = sum(1 for r in rows if r.get("pod_phase") == "Succeeded")
        failed_runs = total_runs - succeeded_runs
        
        # Check minimum repeats
        for load_value, group in groups.items():
            if len(group) < min_repeats:
                warnings.append(
                    f"Load {load_value}: Only {len(group)} runs (min: {min_repeats})"
                )
        
        # Evaluate each load group
        for load_value, group in groups.items():
            # Extract metrics
            error_rates = [self._parse_float(r.get("http_failed_percent", "0")) for r in group]
            checks_rates = [self._parse_float(r.get("checks_pass_percent", "100")) for r in group]
            p95_values = [self._parse_float(r.get("http_p95_ms", "0")) for r in group]
            p99_values = [self._parse_float(r.get("http_p99_ms", "0")) for r in group]
            
            # Calculate statistics
            metrics_summary[load_value] = {
                "error_rate_median": statistics.median(error_rates) if error_rates else 0,
                "checks_pass_median": statistics.median(checks_rates) if checks_rates else 0,
                "p95_median": statistics.median(p95_values) if p95_values else 0,
                "p99_median": statistics.median(p99_values) if p99_values else 0,
            }
            
            # Check SLO violations (use median for robustness)
            error_rate_median = statistics.median(error_rates) if error_rates else 0
            if error_rate_median > thresholds["max_error_rate_percent"]:
                violations.append(Violation(
                    metric="error_rate_percent",
                    threshold=thresholds["max_error_rate_percent"],
                    actual=error_rate_median,
                    severity="error",
                    load_value=load_value,
                    message=f"Error rate {error_rate_median:.2f}% exceeds threshold {thresholds['max_error_rate_percent']}%",
                ))
            
            checks_median = statistics.median(checks_rates) if checks_rates else 0
            if checks_median < thresholds["min_checks_pass_rate_percent"]:
                violations.append(Violation(
                    metric="checks_pass_percent",
                    threshold=thresholds["min_checks_pass_rate_percent"],
                    actual=checks_median,
                    severity="error",
                    load_value=load_value,
                    message=f"Checks pass rate {checks_median:.2f}% below threshold {thresholds['min_checks_pass_rate_percent']}%",
                ))
            
            p95_median = statistics.median(p95_values) if p95_values else 0
            if p95_median > thresholds["max_p95_latency_ms"]:
                violations.append(Violation(
                    metric="p95_latency_ms",
                    threshold=thresholds["max_p95_latency_ms"],
                    actual=p95_median,
                    severity="error",
                    load_value=load_value,
                    message=f"p95 latency {p95_median:.2f}ms exceeds threshold {thresholds['max_p95_latency_ms']}ms",
                ))
            
            p99_median = statistics.median(p99_values) if p99_values else 0
            if p99_median > thresholds["max_p99_latency_ms"]:
                violations.append(Violation(
                    metric="p99_latency_ms",
                    threshold=thresholds["max_p99_latency_ms"],
                    actual=p99_median,
                    severity="error",
                    load_value=load_value,
                    message=f"p99 latency {p99_median:.2f}ms exceeds threshold {thresholds['max_p99_latency_ms']}ms",
                ))
            
            # Check dropped iterations
            if "dropped_iterations_total" in group[0]:
                dropped = [self._parse_float(r.get("dropped_iterations_total", "0")) for r in group]
                iterations = [self._parse_float(r.get("iterations_total", "1")) for r in group]
                dropped_rates = [
                    (d / i * 100) if i > 0 else 0
                    for d, i in zip(dropped, iterations)
                ]
                dropped_median = statistics.median(dropped_rates) if dropped_rates else 0
                
                if dropped_median > thresholds["max_dropped_iterations_rate_percent"]:
                    violations.append(Violation(
                        metric="dropped_iterations_rate_percent",
                        threshold=thresholds["max_dropped_iterations_rate_percent"],
                        actual=dropped_median,
                        severity="error",
                        load_value=load_value,
                        message=f"Dropped iterations rate {dropped_median:.2f}% exceeds threshold",
                    ))
            
            # Check TTFT for streaming scenarios
            if "ttft_p95_ms" in group[0] and "max_ttft_p95_ms" in thresholds:
                ttft_values = [self._parse_float(r.get("ttft_p95_ms", "0")) for r in group]
                ttft_median = statistics.median(ttft_values) if ttft_values else 0
                
                if ttft_median > thresholds["max_ttft_p95_ms"]:
                    violations.append(Violation(
                        metric="ttft_p95_ms",
                        threshold=thresholds["max_ttft_p95_ms"],
                        actual=ttft_median,
                        severity="error",
                        load_value=load_value,
                        message=f"TTFT p95 {ttft_median:.2f}ms exceeds threshold {thresholds['max_ttft_p95_ms']}ms",
                    ))
            
            # Stability analysis (coefficient of variation)
            if len(p95_values) >= 2:
                cv_p95 = self._calculate_cv(p95_values)
                stability_key = f"{load_value}_p95_cv"
                stability_analysis[stability_key] = cv_p95
                
                if cv_p95 > max_cv:
                    warnings.append(
                        f"Load {load_value}: p95 latency CV={cv_p95:.2f}% exceeds {max_cv}% — results may be unreliable"
                    )
            
            if len(error_rates) >= 2:
                cv_error = self._calculate_cv(error_rates)
                stability_key = f"{load_value}_error_rate_cv"
                stability_analysis[stability_key] = cv_error
                
                if cv_error > max_cv * 2:  # Allow more variance in error rate
                    warnings.append(
                        f"Load {load_value}: Error rate CV={cv_error:.2f}% — high variance"
                    )
        
        # Determine verdict
        if not rows:
            verdict = Verdict.INCOMPLETE
        elif any(v.severity == "error" for v in violations):
            verdict = Verdict.FAILED
        elif any(cv > max_cv for cv in stability_analysis.values() if "_p95_cv" in str(cv)):
            verdict = Verdict.UNSTABLE
        else:
            verdict = Verdict.PASSED
        
        return EvaluationResult(
            verdict=verdict,
            scenario=scenario,
            gateway=gateway,
            total_runs=total_runs,
            succeeded_runs=succeeded_runs,
            failed_runs=failed_runs,
            violations=violations,
            warnings=warnings,
            metrics_summary=metrics_summary,
            stability_analysis=stability_analysis,
        )
    
    def compare_gateways(
        self,
        results: Dict[str, EvaluationResult],
    ) -> Dict[str, Any]:
        """
        Compare evaluation results across multiple gateways.
        
        Args:
            results: Dict mapping gateway name to EvaluationResult
        
        Returns:
            Comparison summary with winner determination
        """
        if len(results) < 2:
            return {"error": "Need at least 2 gateways to compare"}
        
        comparison = {
            "gateways": list(results.keys()),
            "scenario": list(results.values())[0].scenario,
            "verdicts": {gw: r.verdict.value for gw, r in results.items()},
            "metrics_comparison": {},
            "winner": None,
            "analysis": [],
        }
        
        # Find common load values
        all_load_values = set()
        for r in results.values():
            all_load_values.update(r.metrics_summary.keys())
        
        for load_value in sorted(all_load_values, key=lambda x: float(x) if x.isdigit() else 0):
            comparison["metrics_comparison"][load_value] = {}
            
            for gw, r in results.items():
                if load_value in r.metrics_summary:
                    comparison["metrics_comparison"][load_value][gw] = r.metrics_summary[load_value]
        
        # Determine winner based on p95 latency and error rate
        scores = {}
        for gw, r in results.items():
            if r.verdict == Verdict.PASSED:
                # Lower is better for latency and error rate
                avg_p95 = statistics.mean([
                    m.get("p95_median", float("inf"))
                    for m in r.metrics_summary.values()
                ])
                avg_error = statistics.mean([
                    m.get("error_rate_median", float("inf"))
                    for m in r.metrics_summary.values()
                ])
                scores[gw] = avg_p95 + (avg_error * 100)  # Weight error rate higher
            else:
                scores[gw] = float("inf")
        
        if scores:
            winner = min(scores, key=scores.get)
            if scores[winner] < float("inf"):
                comparison["winner"] = winner
                comparison["analysis"].append(
                    f"{winner} has the best combination of low latency and low error rate"
                )
        
        return comparison


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Evaluate benchmark results against SLOs"
    )
    parser.add_argument("summary_csv", help="Path to summary.csv")
    parser.add_argument("--scenario", required=True, help="Scenario name")
    parser.add_argument("--gateway", default="kong", help="Gateway name")
    parser.add_argument("--config", help="Path to benchmark_config.yaml")
    parser.add_argument("--output", choices=["json", "markdown", "text"], default="text")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any warning")
    
    args = parser.parse_args()
    
    evaluator = BenchmarkEvaluator(args.config)
    result = evaluator.evaluate_campaign(
        args.summary_csv,
        args.scenario,
        args.gateway,
    )
    
    if args.output == "json":
        print(json.dumps(result.to_dict(), indent=2))
    elif args.output == "markdown":
        print(result.to_markdown())
    else:
        # Text output
        print(f"\n{'='*60}")
        print(f"  Benchmark Evaluation: {result.scenario}")
        print(f"  Gateway: {result.gateway}")
        print(f"{'='*60}")
        print(f"\n  Verdict: {result.verdict.value}")
        print(f"  Runs: {result.succeeded_runs}/{result.total_runs} succeeded\n")
        
        if result.violations:
            print("  Violations:")
            for v in result.violations:
                print(f"    ❌ {v.metric}: {v.actual:.2f} (threshold: {v.threshold:.2f})")
        
        if result.warnings:
            print("\n  Warnings:")
            for w in result.warnings:
                print(f"    ⚠️  {w}")
        
        print()
    
    # Exit code
    if result.verdict == Verdict.FAILED:
        sys.exit(1)
    elif result.verdict == Verdict.UNSTABLE and args.strict:
        sys.exit(2)
    elif result.warnings and args.strict:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
