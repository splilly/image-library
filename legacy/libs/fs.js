var path = require('path'),
    fs   = require('fs');

fs.readdirAsync = function(dir) {
  return new Promise(function(resolve, reject) {
    fs.readdir(dir, function(err, list) {
      if (err) {
        reject(err);
      } else {
        resolve(list);
      }
    });
  });
}

fs.statAsync = function(file) {
  return new Promise(function(resolve, reject) {
    fs.stat(file, function(err, stat) {
      if (err) {
        reject(err);
      } else {
        resolve(stat);
      }
    });
  });
}

function *findFilesGenerator(dir, filter) {
  console.log("Looking for files in " + dir);
  // default filter function accepts all files
  filter = filter || function() {return true;};

  var list = fs.readdirSync(dir);
  var file, stat;
  for (var i = 0; i < list.length; i++) {
    file = path.resolve(dir, list[i]);
    try {
      stat = fs.statSync(file);
      if (stat.isDirectory()) {
        yield *findFilesGenerator(file, filter);
      } else if (filter(file)) {
        yield file;
      }
    } catch (e) {
      console.warn("Failed to scan " + file);
    }
  }
}

function fileFilesRecursive(dir, filter) {
  console.log("Looking for files in " + dir);
  // default filter function accepts all files
  filter = filter || function() {return true;};
  return fs.readdirAsync(dir).then(function(list) {
    return Promise.all(list.map(function(file) {
      file = path.resolve(dir, file);
      return fs.statAsync(file).then(function(stat) {
        if (stat.isDirectory()) {
          return fileFilesRecursive(file, filter);
        } else {
          return filter(file) ? file : "";
        }
      });
    })).then(function(results) {
      return results.filter(function(f) {
        return !!f;
      });
    });
  }).then(function(results) {
    console.log("Found " + results.length + " files in " + dir);
    // flatten the array of arrays
    return Array.prototype.concat.apply([], results);
  });
}

module.exports.findFilesInDir = findFilesGenerator;
