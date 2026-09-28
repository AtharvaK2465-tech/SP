CC ?= gcc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Wpedantic
PORT ?= 8080
CFLAGS += -D_GNU_SOURCE -DPORT=$(PORT)
LDFLAGS ?= -pthread
TARGET=syswatch
SRC=src/main.c

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -Iinclude -o $@ $(SRC) $(LDFLAGS)

clean:
	rm -f $(TARGET)

valgrind: $(TARGET)
	valgrind --leak-check=full --track-origins=yes ./$(TARGET)
