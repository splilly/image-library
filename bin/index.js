#!/usr/bin/env node

var program = require('commander');
var chalk = require('chalk');

program
  .version("1.0.0")
  .option('-i, --inputDir <input>', 'The input library directory')
  .option('-o, --outputDir <output>', 'The output library directory')
  .option('-d, --dryrun', 'Pretend to process the library but don\'t change any files')
  .parse(process.argv);

var error = "";
error += (typeof program.inputDir === 'undefined') ? "\n  inputDir is required\n" : "";
error += (typeof program.outputDir === 'undefined') ? "\n  outputDir is required\n" : "";

if (error !== "") {
  program.outputHelp(function(txt) {
    return chalk.red(error) + txt;
  });
  return;
}

/*var cleanup = require('./bin/cleanup');
cleanup.execute(program.inputDir, program.outputDir, program.dryrun, function(err) {
  if (err) {
    return console.error(err);
  }
  console.log('Complete!');
});*/

var scan = require('./scan');
scan.execute(program.inputDir, program.outputDir, program.dryRun, function(err) {
  if (err) {
    return console.error(err);
  }
  console.log('Complete!');
})
